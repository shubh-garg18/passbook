"""Payload construction and push semantics. SPEC §7.2, §10.

The API is mocked via httpx.MockTransport. Nothing here touches the network.
"""

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from passbook.config import token_expiry
from passbook.firefly.client import (
    DuplicateTransaction,
    FireflyClient,
    FireflyError,
    ValidationFailed,
)
from passbook.firefly.push import build_payload, push_transactions
from passbook.models import UPI, Transaction

ASSET = "Canara savings"


def txn(**kw) -> Transaction:
    base = dict(
        txn_id="20260509000001",
        txn_date=date(2026, 5, 9),
        narration="UPI/DR/412345678901/ZOKVEX QI/YESB/**12345@YBL/UPI//X/09/05/2026 01:51:33",
        debit=Decimal("65.00"),
        credit=None,
        balance=Decimal("12547.64"),
        channel=UPI,
        payee="ZOKVEX QI",
    )
    base.update(kw)
    return Transaction(**base)


# --- payload shape ------------------------------------------------------------


def test_withdrawal_puts_the_asset_account_on_the_source_side():
    split = build_payload(txn(), ASSET)["transactions"][0]
    assert split["type"] == "withdrawal"
    assert split["source_name"] == ASSET
    assert split["destination_name"] == "ZOKVEX QI"


def test_deposit_reverses_the_sides():
    split = build_payload(
        txn(debit=None, credit=Decimal("48.00")), ASSET
    )["transactions"][0]
    assert split["type"] == "deposit"
    assert split["source_name"] == "ZOKVEX QI"
    assert split["destination_name"] == ASSET


def test_amount_is_a_positive_string_never_a_float():
    """CLAUDE.md non-negotiable #1: money is Decimal, never float."""
    split = build_payload(txn(), ASSET)["transactions"][0]
    assert split["amount"] == "65.00"
    assert isinstance(split["amount"], str)
    assert Decimal(split["amount"]) > 0
    assert "float" not in str(type(split["amount"]))
    # and it must survive json round-tripping without becoming a float
    assert isinstance(json.loads(json.dumps(split))["amount"], str)


def test_deposit_amount_is_also_positive():
    split = build_payload(txn(debit=None, credit=Decimal("48.00")), ASSET)["transactions"][0]
    assert Decimal(split["amount"]) == Decimal("48.00")


def test_narration_is_preserved_verbatim_in_notes():
    t = txn()
    split = build_payload(t, ASSET)["transactions"][0]
    assert split["notes"] == t.narration  # byte-for-byte, always


def test_external_id_is_the_banks_own_transaction_id():
    split = build_payload(txn(), ASSET)["transactions"][0]
    assert split["external_id"] == "20260509000001"


def test_currency_is_inr():
    assert build_payload(txn(), ASSET)["transactions"][0]["currency_code"] == "INR"


def test_dedup_and_rules_flags_are_set():
    payload = build_payload(txn(), ASSET)
    assert payload["error_if_duplicate_hash"] is True
    assert payload["apply_rules"] is True


def test_description_combines_payee_and_channel():
    assert build_payload(txn(), ASSET)["transactions"][0]["description"] == "ZOKVEX QI (UPI)"


def test_missing_payee_becomes_unknown_channel():
    """SPEC §7.2: `"Unknown (<channel>)"` when narration yielded no payee."""
    split = build_payload(txn(payee=None), ASSET)["transactions"][0]
    assert split["destination_name"] == "Unknown (UPI)"
    assert split["description"] == "Unknown (UPI)"


def test_reversal_is_tagged_and_still_posts_as_a_deposit():
    split = build_payload(
        txn(debit=None, credit=Decimal("48.00"), payee=None, is_reversal=True), ASSET
    )["transactions"][0]
    assert split["type"] == "deposit"  # Firefly nets it correctly
    assert "reversal" in split["tags"]


def test_large_oneoff_is_never_tagged_client_side():
    """SPEC D5: the parser normalises, Firefly classifies.

    Tagging this here cannot honour §8's exclusions — the pusher does not know
    which category a row will land in. Doing so tagged the fund purchase and
    the card payment, the exact two rows the rule was meant to skip.
    """
    for amount in (Decimal("15000.00"), Decimal("10000.00"), Decimal("999999.00")):
        split = build_payload(txn(debit=amount), ASSET)["transactions"][0]
        assert "large-oneoff" not in split.get("tags", [])


# --- client behaviour ---------------------------------------------------------


def make_client(handler, **kw) -> FireflyClient:
    return FireflyClient(
        "http://firefly.test", "unused-token",
        client=httpx.Client(transport=httpx.MockTransport(handler)), **kw
    )


def validation_error(message: str, status: int = 422) -> httpx.Response:
    return httpx.Response(
        status, json={"message": message, "errors": {"transactions.0.description": [message]}}
    )


def test_duplicate_is_detected_by_message_not_by_error_key():
    """The trap, verified against the live instance.

    A genuine validation failure lands under the SAME `transactions.0.description`
    key as a duplicate — an empty POST returns "Need at least one transaction."
    there. Keying on the field name would silently swallow real errors as
    duplicates.
    """
    client = make_client(lambda r: validation_error("Duplicate of transaction #42."))
    with pytest.raises(DuplicateTransaction):
        client.store_transaction({})

    client = make_client(lambda r: validation_error("Need at least one transaction."))
    with pytest.raises(ValidationFailed) as exc:
        client.store_transaction({})
    assert not isinstance(exc.value, DuplicateTransaction)


def test_real_validation_failure_is_not_counted_as_a_duplicate():
    client = make_client(lambda r: validation_error("The amount field is required."))
    result = push_transactions(client, [txn()], ASSET)
    assert result.duplicates == 0
    assert result.failed == 1
    assert not result.ok


def test_duplicates_are_counted_and_do_not_stop_the_run():
    """Duplicate rejection is a normal outcome on overlapping downloads."""
    client = make_client(lambda r: validation_error("Duplicate of transaction #7."))
    result = push_transactions(client, [txn(), txn(), txn()], ASSET)
    assert (result.pushed, result.duplicates, result.failed) == (0, 3, 0)
    assert result.ok


def test_successful_pushes_are_counted():
    client = make_client(lambda r: httpx.Response(200, json={"data": {"id": "1"}}))
    result = push_transactions(client, [txn(), txn()], ASSET)
    assert (result.pushed, result.duplicates, result.failed) == (2, 0, 0)


def test_mixed_run_reports_each_category():
    seen = {"n": 0}

    def handler(request):
        seen["n"] += 1
        if seen["n"] == 1:
            return httpx.Response(200, json={"data": {}})
        if seen["n"] == 2:
            return validation_error("Duplicate of transaction #7.")
        return validation_error("Something else went wrong.")

    result = push_transactions(make_client(handler), [txn(), txn(), txn()], ASSET)
    assert (result.pushed, result.duplicates, result.failed) == (1, 1, 1)


def test_server_errors_are_retried_then_raised(monkeypatch):
    monkeypatch.setattr("passbook.firefly.client.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, json={"message": "upstream down"})

    client = make_client(handler, retries=3)
    with pytest.raises(FireflyError) as exc:
        client.store_transaction({})
    assert calls["n"] == 3
    assert exc.value.status == 503


def test_retry_gives_up_and_succeeds_if_the_server_recovers(monkeypatch):
    monkeypatch.setattr("passbook.firefly.client.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={"message": "flaky"})
        return httpx.Response(200, json={"data": {"id": "9"}})

    assert make_client(handler, retries=3).store_transaction({}) == {"data": {"id": "9"}}


def test_client_errors_are_not_retried(monkeypatch):
    monkeypatch.setattr("passbook.firefly.client.time.sleep", lambda _s: None)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(404, json={"message": "nope"})

    with pytest.raises(FireflyError):
        make_client(handler, retries=3).store_transaction({})
    assert calls["n"] == 1, "4xx must not be retried"


def test_401_explains_the_expiry_possibility():
    client = make_client(lambda r: httpx.Response(401, json={"message": "Unauthenticated."}))
    with pytest.raises(FireflyError, match="365 days"):
        client.store_transaction({})


def test_error_never_contains_the_token():
    secret = "eyJhbGciOiJSUzI1NiJ9.SECRETTOKENVALUE.sig"
    client = FireflyClient(
        "http://firefly.test", secret,
        client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(422, json={"message": "bad"}))),
    )
    with pytest.raises(FireflyError) as exc:
        client.store_transaction({})
    assert secret not in str(exc.value)
    assert "SECRETTOKENVALUE" not in repr(exc.value.body)


# --- token expiry (no API call) -----------------------------------------------


def _jwt(exp: int) -> str:
    import base64

    def seg(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{seg({'alg': 'RS256'})}.{seg({'exp': exp})}.signature"


def test_expiry_is_read_from_the_exp_claim():
    from datetime import datetime, timezone

    when = datetime(2027, 8, 7, 12, 0, tzinfo=timezone.utc)
    assert token_expiry(_jwt(int(when.timestamp()))) == when


def test_non_jwt_tokens_yield_no_expiry():
    """The 'Command line token' is 32 chars with no dots — the wrong credential."""
    assert token_expiry("0123456789abcdef0123456789abcdef") is None
    assert token_expiry("") is None
    assert token_expiry("a.b") is None


def test_malformed_payload_does_not_raise():
    assert token_expiry("header.!!!not-base64!!!.sig") is None
    assert token_expiry("aGVsbG8.aGVsbG8.sig") is None  # valid base64, not JSON


def test_jwt_without_an_exp_claim_yields_none():
    import base64

    payload = base64.urlsafe_b64encode(json.dumps({"sub": "1"}).encode()).rstrip(b"=").decode()
    assert token_expiry(f"aGVsbG8.{payload}.sig") is None
