"""Schema versions and migrations. SPEC §22.2.

The property under test is not "migrations run". It is **"a marker never
outranks the ledger"** — because that is the §19 failure in miniature: a stated
fact nobody checked, rendering as a pass.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from passbook import migrate


# ── discovery ────────────────────────────────────────────────────────────────

def test_the_baseline_migration_ships():
    steps = migrate.all_migrations()
    assert [s.version for s in steps] == sorted(s.version for s in steps)
    assert steps[0].version == 1
    assert steps[0].name == "namespace-external-ids"
    assert migrate.schema_version() == steps[-1].version


def test_every_migration_describes_itself():
    """`make upgrade` prints these to someone deciding whether to run it."""
    for step in migrate.all_migrations():
        assert step.name and step.name.islower()
        assert len(step.description) > 40, f"{step.name} needs a real description"


# ── the marker is a record, not the authority ────────────────────────────────

def test_an_unrecorded_install_reads_none_not_zero(tmp_path):
    """None and 0 are different situations: a pre-marker install and a genuinely
    empty one. Only detection can tell them apart, so the marker must not
    pretend to."""
    assert migrate.recorded_version(tmp_path / "absent") is None


def test_a_corrupt_marker_reads_as_unrecorded(tmp_path, caplog):
    marker = tmp_path / "schema-version"
    marker.write_text("not a number\n")
    assert migrate.recorded_version(marker) is None


def test_recording_round_trips_and_explains_itself(tmp_path):
    marker = tmp_path / "schema-version"
    migrate.record_version(7, marker)
    assert migrate.recorded_version(marker) == 7
    body = marker.read_text()
    assert body.splitlines()[0] == "7"
    assert "Detection does not trust this file" in body


# ── pending() ────────────────────────────────────────────────────────────────

def _ctx(**overrides):
    fields = dict(
        settings=None, client=None, registry=[], say=lambda m: None,
        purge_and_repush=lambda a: None, statement_paths=lambda a: [],
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _step(version, *, pending, name="fake"):
    return migrate.Step(
        version=version, name=name, description="d" * 50,
        pending=pending, run=lambda ctx: None, verify=lambda ctx: None,
    )


def test_pending_asks_every_step_even_ones_the_marker_claims_are_done(monkeypatch):
    asked = []

    def watcher(ctx):
        asked.append(True)
        return None

    monkeypatch.setattr(migrate, "all_migrations",
                        lambda: [_step(1, pending=watcher), _step(2, pending=watcher)])
    migrate.pending(_ctx())
    assert len(asked) == 2, "a marker is a hint; every step still gets asked"


def test_a_step_that_cannot_tell_is_reported_as_pending(monkeypatch):
    """"could not determine" must never render as "nothing to do". That is the
    tri-state rule from §20.2 applied to migrations."""
    def explodes(ctx):
        raise RuntimeError("Firefly is asleep")

    monkeypatch.setattr(migrate, "all_migrations", lambda: [_step(1, pending=explodes)])
    outstanding = migrate.pending(_ctx())
    assert len(outstanding) == 1
    assert "could not determine" in outstanding[0][1]
    assert "Firefly is asleep" in outstanding[0][1]


def test_pending_is_returned_in_run_order(monkeypatch):
    monkeypatch.setattr(migrate, "all_migrations", lambda: [
        _step(2, pending=lambda ctx: "second", name="b"),
        _step(1, pending=lambda ctx: "first", name="a"),
    ])
    # all_migrations is what sorts; pending preserves that order.
    assert [s.version for s, _ in migrate.pending(_ctx())] == [2, 1]
    assert [s.version for s in migrate.all_migrations()] == [2, 1]


def test_duplicate_versions_are_a_hard_error(monkeypatch):
    import passbook.migrations as package

    class FakeModule:
        VERSION = 1
        NAME = "clash"
        DESCRIPTION = "d" * 50
        pending = staticmethod(lambda ctx: None)
        run = staticmethod(lambda ctx: None)
        verify = staticmethod(lambda ctx: None)

    monkeypatch.setattr(
        migrate.pkgutil, "iter_modules",
        lambda paths: [SimpleNamespace(name="m001_a"), SimpleNamespace(name="m001_b")],
    )
    monkeypatch.setattr(migrate.importlib, "import_module", lambda name: FakeModule)
    with pytest.raises(RuntimeError, match="duplicate migration versions"):
        migrate.all_migrations()


# ── the baseline migration's own detection ───────────────────────────────────

class FakeClient:
    def __init__(self, external_ids):
        self._ids = external_ids

    def asset_accounts(self):
        return [{"id": "7", "attributes": {"name": "Savings"}}]

    def account_transactions(self, account_id):
        assert account_id == "7"
        return [
            {"attributes": {"transactions": [{"external_id": external}]}}
            for external in self._ids
        ]


def _account(slug="canara-1111"):
    from passbook.config import Account

    return Account(slug=slug, bank="canara", account_number="999900001111",
                   asset_account="Savings")


def test_the_baseline_is_a_no_op_on_a_ledger_that_is_already_namespaced():
    from passbook.migrations import m001_namespace_external_ids as m001

    ctx = _ctx(client=FakeClient(["canara-1111-20260509000001"]), registry=[_account()])
    assert m001.pending(ctx) is None
    assert m001.verify(ctx) is None


def test_the_baseline_detects_bare_ids_and_says_how_many():
    from passbook.migrations import m001_namespace_external_ids as m001

    ctx = _ctx(
        client=FakeClient(["20260509000001", "20260509000002", "canara-1111-20260510000001"]),
        registry=[_account()],
    )
    reason = m001.pending(ctx)
    assert reason is not None
    assert "2 row(s)" in reason
    assert "canara-1111: 2" in reason
    assert "collide" in reason, "the reason has to say why it matters"


def test_the_baseline_reads_the_ledger_not_a_version_file(tmp_path):
    """Recording version 1 does not make a ledger full of bare ids clean."""
    from passbook.migrations import m001_namespace_external_ids as m001

    marker = tmp_path / "schema-version"
    migrate.record_version(1, marker)
    assert migrate.recorded_version(marker) == 1

    ctx = _ctx(client=FakeClient(["20260509000001"]), registry=[_account()])
    assert m001.pending(ctx) is not None, "the marker must not outrank the rows"


def test_the_baseline_never_deletes_rows_it_cannot_rebuild():
    """archive/ empty plus rows in Firefly is data loss with a progress bar."""
    from passbook.migrations import m001_namespace_external_ids as m001

    calls = []
    ctx = _ctx(
        client=FakeClient(["20260509000001"]),
        registry=[_account()],
        purge_and_repush=lambda account: calls.append(account),
    )
    m001.run(ctx)
    assert calls == [ctx.registry[0]], "run() delegates; it owns no delete of its own"
