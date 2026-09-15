"""The in-place sync, over HTTP. SPEC §24.

`test_sync_ledger.py` covers the service layer. This covers the two things only
the route can get wrong, and both of them were found this way rather than by
reading the code:

* a helper the response builder needs that nobody imported — `/reapply` raised
  `NameError` at request time and every service-level test still passed,
  because none of them builds a response;
* the shape the browser actually receives, which is the contract the pages are
  written against.
"""

from __future__ import annotations

from datetime import date

import pytest

# The web fixtures live in `test_web.py` rather than `conftest.py`. Importing
# them re-registers them here, which is pytest's own mechanism for sharing a
# fixture between modules without moving it.
from test_web import api, app, signed_in  # noqa: F401
# A package attribute is not a patch seam: a route resolves the name in its
# own module's globals, so the fake goes on `_base` — the one place a store is
# ever opened.
from passbook.web.api import _base as _api_base


@pytest.fixture
def ledger(monkeypatch):
    """A ledger holding one account and no rows."""

    class FakeLedger:
        instances: list = []

        def __init__(self, *args, **kwargs):
            self.updates: list[tuple[str, dict]] = []
            FakeLedger.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            return None

        def asset_accounts(self):
            # `current_balance` and `opening_balance` are not optional: the
            # verdict reads both, and a fake without them fails inside a route
            # rather than in the test.
            return [
                {
                    "name": "Test Account",
                    "current_balance": "5068.09",
                    "opening_balance": "12612.64",
                    "opening_on": date(2026, 5, 6),
                    "currency": "INR",
                }
            ]

        def account_transactions(self, account):
            return []

        def identities(self, account):
            return set()

        def update_transaction(self, external_id, fields):
            self.updates.append((external_id, fields))

    FakeLedger.instances = []
    monkeypatch.setattr(_api_base, "open_ledger", FakeLedger)
    return FakeLedger


def test_reapply_builds_a_response_rather_than_raising(signed_in, ledger):
    """The regression: `_preview` calls `_change`, which was never ported.

    A `NameError` inside a route is a 500 the whole page shows as "something
    went wrong", and no service-level test can reach it.
    """
    response = signed_in.get("/reapply")
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    for key in ("considered", "renames", "recats", "counterparties", "retags", "changes"):
        assert key in body, f"{key} missing from /reapply"


def test_reapply_sync_answers_with_what_it_verified(signed_in, ledger):
    """`remaining` is a claim about the ledger; `updated` is a claim about the
    requests. Only the first may make `ok` true (non-negotiable 11)."""
    response = signed_in.post("/reapply/sync")
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["considered"] == 0
    assert body["updated"] == 0
    assert body["remaining"] == 0
    assert set(body) >= {"ok", "considered", "attempted", "updated", "failed", "remaining"}


def test_nothing_compared_is_never_reported_as_ok(signed_in, ledger, monkeypatch):
    """§24.1's exact shape: a join that matched nothing said "all match".

    `remaining` of `None` is *unverified* — the third state — and must not make
    `ok` true even when zero rows failed.
    """
    from passbook.web.api import reapply as _api_reapply

    monkeypatch.setattr(
        _api_reapply,
        "_sync_now",
        lambda client, st: {
            "considered": 0,
            "attempted": 0,
            "updated": 0,
            "failed": 0,
            "failures": [],
            "remaining": None,
        },
    )
    body = signed_in.post("/reapply/sync").get_json()
    assert body["remaining"] is None
    assert body["ok"] is False


def test_a_rename_reaches_rules_yaml_in_the_same_request(signed_in, ledger, tmp_path, monkeypatch):
    """§24.4. A rule matches the display name, so relabelling a payee without
    rewriting its entry silently de-categorises it."""
    from passbook import configwrite

    rules = tmp_path / "rules.yaml"
    rules.write_text(
        "rules:\n"
        "  - category: Eating Out\n"
        "    payees: [Canteen]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(configwrite, "RULES_FILE", rules)

    change = configwrite.plan_categories({}, {"CANTEEN X": "Mess"}, renames={"Canteen": "Mess"})
    change.apply()

    assert "Mess" in rules.read_text()
    assert "Canteen" not in rules.read_text()
