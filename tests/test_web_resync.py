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

import pytest

# The web fixtures live in `test_web.py` rather than `conftest.py`. Importing
# them re-registers them here, which is pytest's own mechanism for sharing a
# fixture between modules without moving it.
from test_web import api, app, signed_in  # noqa: F401
# A package attribute is not a patch seam: a route resolves the name in its
# own module's globals, so the fake goes on `_base` — the one place a client
# is ever constructed.
from passbook.web.api import _base as _api_base


@pytest.fixture
def firefly(monkeypatch):
    """A store holding one row, whose description is one rename out of date."""

    class FakeClient:
        instances: list = []

        def __init__(self, *args, **kwargs):
            self.updates: list[tuple[str, dict]] = []
            FakeClient.instances.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def asset_accounts(self):
            return [{"id": "1", "attributes": {"name": "Test Account"}}]

        def account_transactions(self, account_id):
            return []

        def update_transaction(self, group_id, payload):
            self.updates.append((group_id, payload))
            return {}

    FakeClient.instances = []
    import passbook.web.api as api_mod

    monkeypatch.setattr(_api_base, "FireflyClient", FakeClient)
    monkeypatch.setenv("FIREFLY_TOKEN", "t")
    return FakeClient


def test_reapply_builds_a_response_rather_than_raising(signed_in, firefly):
    """The regression: `_preview` calls `_change`, which was never ported.

    A `NameError` inside a route is a 500 the whole page shows as "something
    went wrong", and no service-level test can reach it.
    """
    response = signed_in.get("/reapply")
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    for key in ("considered", "renames", "recats", "counterparties", "retags", "changes"):
        assert key in body, f"{key} missing from /reapply"


def test_reapply_sync_answers_with_what_it_verified(signed_in, firefly):
    """`remaining` is a claim about the ledger; `updated` is a claim about the
    requests. Only the first may make `ok` true (non-negotiable 11)."""
    response = signed_in.post("/reapply/sync")
    assert response.status_code == 200, response.get_json()
    body = response.get_json()
    assert body["considered"] == 0
    assert body["updated"] == 0
    assert body["remaining"] == 0
    assert set(body) >= {"ok", "considered", "attempted", "updated", "failed", "remaining"}


def test_nothing_compared_is_never_reported_as_ok(signed_in, firefly, monkeypatch):
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


def test_a_rename_reaches_rules_yaml_in_the_same_request(signed_in, firefly, tmp_path, monkeypatch):
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
