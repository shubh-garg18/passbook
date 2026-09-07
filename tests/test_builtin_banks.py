"""Profiles that ship with passbook. SPEC §55.

A shipped profile is the difference between "add your bank" being a step and
being a fallback: a fresh clone reads a bank nobody on that machine has ever
seen. That only works if every one of them is correct, because a broken profile
in the package is broken for everybody and cannot be fixed by the person hitting
it.

So each is loaded, validated and checked for the mistakes that are easy to make
in YAML and invisible until an import fails.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from passbook.loaders import _table
from passbook.loaders.profiles import BUILTIN_DIR, FIELDS, META_FIELDS, load_profiles

SHIPPED = sorted(BUILTIN_DIR.glob("*.yaml"))


def test_there_is_at_least_one():
    """If this ever empties, the registry has been deleted rather than emptied
    on purpose — say so here first."""
    assert SHIPPED, f"no profiles in {BUILTIN_DIR}"


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_it_loads(path: Path):
    """Through the real loader, which is what validates it."""
    loaded = [p for p in load_profiles(Path("/nonexistent")) if p["path"] == path]
    assert len(loaded) == 1, f"{path.name} did not load"


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_it_names_only_real_fields(path: Path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert set(data.get("columns", {}).values()) <= set(FIELDS)
    assert set(data.get("metadata", {}).values()) <= set(META_FIELDS)


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_it_maps_every_column_a_balance_chain_needs(path: Path):
    """Five of the six. `txn_id` is the one a bank may genuinely not print
    (§44.4), and a blank cell in a mapped reference column derives an id
    anyway — so mapping it is optional either way."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert _table.CORE_COLS <= set(data.get("columns", {}).values()), (
        f"{path.name} cannot check a balance chain without all of {_table.CORE_COLS}"
    )


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_it_says_where_the_account_number_is(path: Path):
    """Never derived — it routes every row to a ledger and a wrong one is
    silent forever (§21.1). A shipped profile that omits it refuses at import
    on somebody else's machine, which is the worst place to find out."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "account_number" in set(data.get("metadata", {}).values()), (
        f"{path.name} has no metadata label for account_number"
    )


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_its_date_formats_are_locale_independent(path: Path):
    """`%b` and `%B` read the C locale's month names, which differ between WSL
    and CI — the same trap the built-in Canara matcher avoids with its own month
    table. A shipped profile has no excuse for it. SPEC §27."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for fmt in data.get("dates", []):
        assert "%b" not in fmt and "%B" not in fmt, (
            f"{path.name} declares {fmt!r}; prefer a numeric month"
        )


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_its_declared_dates_actually_parse_something(path: Path):
    """A format nothing can read is a typo that shows up as `unparseable date`
    on a stranger's machine."""
    from datetime import datetime

    for fmt in yaml.safe_load(path.read_text(encoding="utf-8")).get("dates", []):
        rendered = datetime(2026, 4, 6).strftime(fmt)
        assert datetime.strptime(rendered, fmt), fmt


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_a_declared_layout_places_every_column_it_names(path: Path):
    """SPEC §99. `columns_at` and `columns` describe the same six columns from
    two directions — one says where, the other says what to call it — and a
    column named in only one of them is never filled. The rows come out empty
    rather than wrong, which is the harder failure to read."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    at = data.get("columns_at")
    if not at:
        return
    assert set(data.get("columns", {}).values()) <= set(at), (
        f"{path.name} names columns it does not place"
    )


@pytest.mark.parametrize("path", SHIPPED, ids=lambda p: p.stem)
def test_a_declared_layouts_ranges_do_not_overlap(path: Path):
    """A word is placed by the range its centre falls in, so two ranges over
    one point decide nothing — the winner is whichever the dict yields first.
    The loader refuses it; this says so on the shipped ones without waiting for
    somebody's import to fail."""
    at = yaml.safe_load(path.read_text(encoding="utf-8")).get("columns_at") or {}
    ordered = sorted(at.items(), key=lambda pair: pair[1])
    for (left, (_a, a1)), (right, (b0, _b)) in zip(ordered, ordered[1:]):
        assert b0 >= a1, f"{path.name}: {left} and {right} overlap"


def test_the_bank_key_matches_the_filename():
    """`bank:` is what the registry and the UI use; the filename is what a
    person looks for. They drifting apart is a debugging afternoon."""
    for path in SHIPPED:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data.get("bank") == path.stem, f"{path.name} declares {data.get('bank')!r}"


def test_no_two_shipped_profiles_claim_the_same_bank():
    names = [yaml.safe_load(p.read_text(encoding="utf-8"))["bank"] for p in SHIPPED]
    assert len(names) == len(set(names)), names


def test_none_of_them_shadows_a_built_in_loader():
    """`canara` is Python, not a profile. A YAML file claiming it would be
    silently ignored by `route_statement` and confusing forever."""
    from passbook.config import BUILTIN_BANKS

    for path in SHIPPED:
        assert path.stem not in BUILTIN_BANKS


# --- the contract with the operator's own profiles ---------------------------


def test_the_operators_profile_wins_on_a_name_clash(tmp_path):
    """A shipped profile is a default, never a constraint. When a bank re-skins
    its export the operator fixes it once and their version takes over — without
    editing the package, and without a pull reverting it."""
    local = tmp_path / "banks"
    local.mkdir()
    shipped = yaml.safe_load(SHIPPED[0].read_text(encoding="utf-8"))
    (local / f"{shipped['bank']}.yaml").write_text(
        f"bank: {shipped['bank']}\n"
        "columns:\n"
        '  "Mine": date\n'
        '  "Also Mine": narration\n',
        encoding="utf-8",
    )

    found = [p for p in load_profiles(local) if p["bank"] == shipped["bank"]]
    assert len(found) == 1, "both were kept — one bank, two layouts"
    assert found[0]["builtin"] is False
    assert found[0]["columns"] == {"mine": "date", "alsomine": "narration"}, (
        "the shipped columns were merged in rather than replaced"
    )


def test_shipped_banks_are_offered_in_the_ui(tmp_path, monkeypatch):
    """`SUPPORTED_BANKS` drives the Add-account dropdown. A profile that ships
    and is not offered there is a profile nobody can select."""
    from passbook.config import supported_banks

    monkeypatch.setattr("passbook.loaders.profiles.PROFILES_DIR", tmp_path / "none")
    offered = supported_banks()
    for path in SHIPPED:
        assert path.stem in offered, f"{path.stem} ships but is not offered"
