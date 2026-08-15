"""Purge selection and deletion semantics. Mocked API, no network."""

from decimal import Decimal

import httpx
import pytest

from passbook.firefly.client import FireflyClient, FireflyError
from passbook.firefly.purge import Candidate, find_candidates, purge


def group(gid, external_id=None, amount="10.00", description="x", date="2026-05-09"):
    split = {"amount": amount, "description": description, "date": date}
    if external_id:
        split["external_id"] = external_id
    return {"id": str(gid), "attributes": {"transactions": [split]}}


def make_client(handler) -> FireflyClient:
    return FireflyClient(
        "http://firefly.test", "tok",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def listing(groups):
    def handler(request):
        return httpx.Response(
            200,
            json={"data": groups, "meta": {"pagination": {"total_pages": 1}}},
        )

    return handler


# --- selection ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_backups(tmp_path, monkeypatch):
    """`purge()` records intent before deleting (§19.7), and `ops.BACKUPS` is
    CWD-relative — so without this every purge test drops a real intent file into
    the operator's `backups/`, where `verify-ledger` then correctly reports an
    unfinished purge that never happened. Found exactly that way.
    """
    monkeypatch.chdir(tmp_path)
    (tmp_path / "backups").mkdir()


def test_only_rows_with_an_external_id_are_deletable():
    """The opening balance has none, so it is excluded structurally."""
    groups = [group(1), group(2, "20260509000001"), group(3, "20260509000002")]
    candidates, protected = find_candidates(make_client(listing(groups)), 3)
    assert [c.group_id for c in candidates] == ["2", "3"]
    assert len(protected) == 1


def test_an_account_of_only_protected_rows_yields_nothing():
    candidates, protected = find_candidates(make_client(listing([group(1)])), 3)
    assert candidates == []
    assert len(protected) == 1


def test_candidate_carries_enough_to_show_a_dry_run():
    groups = [group(7, "20260509000001", amount="65.00", description="Morning Stall (UPI)")]
    (candidate,), _ = find_candidates(make_client(listing(groups)), 3)
    assert candidate == Candidate(
        group_id="7", external_id="20260509000001", date="2026-05-09",
        description="Morning Stall (UPI)", amount=Decimal("65.00"),
    )


def test_pagination_is_followed():
    pages = {
        1: {"data": [group(1, "a"), group(2, "b")], "meta": {"pagination": {"total_pages": 2}}},
        2: {"data": [group(3, "c")], "meta": {"pagination": {"total_pages": 2}}},
    }

    def handler(request):
        return httpx.Response(200, json=pages[int(request.url.params["page"])])

    candidates, _ = find_candidates(make_client(handler), 3)
    assert [c.group_id for c in candidates] == ["1", "2", "3"]


# --- deletion -----------------------------------------------------------------


def candidates(n):
    return [Candidate(str(i), f"ext{i}", "2026-05-09", "x", Decimal("1")) for i in range(1, n + 1)]


def test_successful_deletes_are_counted():
    result = purge(make_client(lambda r: httpx.Response(204)), candidates(3))
    assert (result.deleted, result.already_gone, result.failed) == (3, 0, 0)
    assert result.ok


def test_trashed_records_are_force_deleted_afterwards():
    """Deleting alone is not enough.

    Firefly soft-deletes, and `TransactionJournalFactory::errorIfDuplicate`
    queries `withTrashed()` — so a tombstone keeps rejecting identical content
    as a duplicate forever. Observed live: after deleting 93 and re-pushing,
    only the 41 rows whose description had changed got through.
    """
    seen = []

    def handler(request):
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(204)

    result = purge(make_client(handler), candidates(2))
    assert "DELETE /api/v1/data/purge" in seen
    assert result.hard_purged is True


def test_nothing_deleted_means_no_force_delete():
    """Don't force-delete other trashed data when this purge did nothing."""
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(204)

    result = purge(make_client(handler), [])
    assert "/api/v1/data/purge" not in seen
    assert result.hard_purged is False


def test_401_with_a_working_token_means_already_gone_not_auth_failure():
    """Firefly 401s for an absent group rather than 404ing, so a bare 401 is
    ambiguous. A live /about proves the token is fine and the group is gone."""

    def handler(request):
        if request.url.path == "/api/v1/about":
            return httpx.Response(200, json={"data": {"version": "6.6.6"}})
        if request.url.path == "/api/v1/data/purge":
            return httpx.Response(204)
        return httpx.Response(401, json={"message": "Unauthenticated."})

    result = purge(make_client(handler), candidates(2))
    assert (result.deleted, result.already_gone, result.failed) == (0, 2, 0)
    assert result.ok  # re-running a completed purge is not an error


def test_401_with_a_dead_token_aborts_rather_than_reporting_success():
    """The failure mode that matters: a token dying mid-purge must not be
    silently recorded as 93 rows already gone."""

    def handler(request):
        return httpx.Response(401, json={"message": "Unauthenticated."})

    with pytest.raises(FireflyError, match="stopped authenticating"):
        purge(make_client(handler), candidates(5))


def test_abort_message_says_how_far_it_got():
    calls = {"n": 0}

    def handler(request):
        if request.url.path == "/api/v1/about":
            # token works for the first check, dies for the second
            calls["n"] += 1
            return httpx.Response(200 if calls["n"] == 1 else 401, json={"data": {}})
        return httpx.Response(401, json={"message": "Unauthenticated."})

    with pytest.raises(FireflyError, match="0 group"):
        purge(make_client(handler), candidates(3))


def test_other_errors_are_collected_and_do_not_stop_the_run(monkeypatch):
    # 5xx is retried with backoff; don't actually sleep through it.
    monkeypatch.setattr("passbook.firefly.client.time.sleep", lambda _s: None)

    def handler(request):
        return httpx.Response(500, json={"message": "boom"})

    result = purge(make_client(handler), candidates(2))
    assert result.failed == 2
    assert not result.ok
    assert len(result.failures) == 2
