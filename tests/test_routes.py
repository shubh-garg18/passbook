"""Every route the package declares is a route the app serves. SPEC §29.

`api.py` was one long module and is now a package of twelve. The blueprint is
shared; what registers a route is **importing the module that declares it**, and
`__init__.py` does that with a hand-written list.

That list is the failure mode. A module nobody imports is a route that quietly
does not exist — no error, no warning, just a 404 on a page that used to work.
So this reads the decorators out of the source and asserts the URL map agrees,
which is exact and needs no maintenance when a route is added.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parent.parent / "src" / "passbook" / "web" / "api"

#: `@api.get("/status")`, `@api.post("/banks/try")`, …
DECORATOR = re.compile(r'^@api\.(get|post|put|delete|patch)\("([^"]+)"\)', re.M)


def declared() -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(PKG.glob("*.py")):
        for verb, rule in DECORATOR.findall(path.read_text(encoding="utf-8")):
            found.add((verb.upper(), rule))
    return found


def served(app) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/api/"):
            continue
        for method in rule.methods - {"HEAD", "OPTIONS"}:
            out.add((method, rule.rule[len("/api") :]))
    return out


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    from passbook.web import create_app

    return create_app({"ARCHIVE": tmp_path_factory.mktemp("archive")})


def test_every_declared_route_is_served(app):
    """The one that catches a module missing from `__init__`."""
    missing = declared() - served(app)
    assert not missing, (
        f"declared but not reachable: {sorted(missing)} — the module that "
        "declares them is probably not imported in api/__init__.py"
    )


def test_every_served_route_is_declared_in_this_package(app):
    """The other direction: nothing registers a route from outside."""
    extra = served(app) - declared()
    assert not extra, sorted(extra)


def test_the_route_count_is_the_one_the_split_produced():
    """A number, so a route lost to a bad merge is caught even when both sides
    of the comparison above lose it together. Counted, not remembered: 36
    `@api` decorators at the split, and the URL map agreed rule for rule before
    and after it — a dropped import in `__init__.py` once left 3. Creating and
    removing a category, the attribution split and the earnings definition took
    it to 44, taking a database backup from the UI to 46, the reminder to 51,
    and emailed sign-in recovery to 53. Moving the ledger in-house took it back
    to 52: `/bootstrap` pushed `rules.yaml` into a separate rules engine, and
    there is no separate rules engine — the rules are applied when a row is
    written."""
    assert len(declared()) == 52


@pytest.mark.parametrize(
    "module",
    [
        "accounts",
        "banks",
        "ledger",
        "ops",
        "payees",
        "reapply",
        "registering",
        "signin",
        "statements",
    ],
)
def test_every_route_module_is_imported_by_the_package(module):
    """Named one by one, so a module dropped from the list fails with its own
    name rather than as an arithmetic difference."""
    import passbook.web.api as package

    assert hasattr(package, module), f"api/__init__.py does not import {module}"


def test_no_module_declares_a_route_without_being_listed():
    """The list and the directory agree. A new module added without being
    imported would otherwise pass every test above by declaring nothing anyone
    looked for."""
    import passbook.web.api as package

    declaring = {
        path.stem
        for path in sorted(PKG.glob("*.py"))
        if DECORATOR.search(path.read_text(encoding="utf-8"))
    }
    for name in declaring:
        assert hasattr(package, name), f"{name}.py declares routes and is not imported"
