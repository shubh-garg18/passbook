"""Schema versions and migrations. SPEC §22.2.

The problem this exists for: `git pull` is silent. Phase 14 changed the shape of
every `external_id` in the ledger, and the correct response was a backup, a
purge, and a resume (§21.2). Someone who pulled that change and simply ran
`make sync` would have got a ledger holding two incompatible id forms and a
`verify-ledger` reporting rows missing — with nothing on screen ever having said
a migration existed.

Three properties, in the order they matter:

1. **Detection is from the data, never from a marker.** Each migration answers
   `pending()` by looking at the actual ledger. A version file that says "you
   are at 1" while the rows say otherwise is exactly the §19 failure — a stated
   fact nobody checked. The marker here is a fast path and a record; it is never
   the authority.
2. **A backup comes first, and it is a precondition rather than advice.**
   `make upgrade` refuses without a dump newer than the run. §18.7 established
   that shape for the purge button and it applies identically here.
3. **Nothing is marked applied until §20 passes.** Same rule as the purge intent
   (§19.7): the record is cleared on the ledger being whole, not on a function
   returning.

Adding one: write `migrations/mNNN_*.py` exposing `VERSION`, `NAME`,
`DESCRIPTION`, `pending(ctx)`, `run(ctx)` and `verify(ctx)`, then bump nothing —
`SCHEMA_VERSION` is derived from the highest module found. See
`docs/upgrading.md`.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

log = logging.getLogger(__name__)

# Per-install state, not repository state: it records what THIS ledger has had
# applied. Lives beside the other operator-owned files in config/, and is
# gitignored with them.
MARKER = Path("config") / "schema-version"


class Migration(Protocol):
    VERSION: int
    NAME: str
    DESCRIPTION: str

    def pending(self, ctx: Any) -> str | None: ...
    def run(self, ctx: Any) -> None: ...
    def verify(self, ctx: Any) -> str | None: ...


@dataclass
class Context:
    """What a migration is handed. Everything dangerous is a callable the CLI
    supplies, so a migration never grows its own copy of the purge path."""

    settings: Any
    client: Any
    registry: list[Any]
    say: Callable[[str], None]
    purge_and_repush: Callable[[Any], None]
    statement_paths: Callable[[Any], list[Path]]


@dataclass(frozen=True)
class Step:
    version: int
    name: str
    description: str
    pending: Callable[[Any], str | None]
    run: Callable[[Any], None]
    verify: Callable[[Any], str | None]


def all_migrations() -> list[Step]:
    """Every migration module, in version order."""
    from . import migrations as package

    steps: list[Step] = []
    for info in sorted(pkgutil.iter_modules(package.__path__), key=lambda i: i.name):
        if not info.name.startswith("m"):
            continue
        module = importlib.import_module(f"{package.__name__}.{info.name}")
        steps.append(
            Step(
                version=module.VERSION,
                name=module.NAME,
                description=module.DESCRIPTION,
                pending=module.pending,
                run=module.run,
                verify=module.verify,
            )
        )
    seen = [s.version for s in steps]
    if len(set(seen)) != len(seen):
        raise RuntimeError(f"duplicate migration versions: {seen}")
    return sorted(steps, key=lambda s: s.version)


def schema_version() -> int:
    """The version this checkout knows how to reach."""
    steps = all_migrations()
    return steps[-1].version if steps else 0


def recorded_version(marker: Path | None = None) -> int | None:
    """What this installation last recorded, or None if it never has.

    None is not zero. A pre-marker install and a genuinely empty one are
    different situations, and only detection can tell them apart.
    """
    path = marker or MARKER
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text.splitlines()[0])
    except (ValueError, IndexError):
        log.warning("%s is unreadable; treating this install as unrecorded", path)
        return None


def record_version(version: int, marker: Path | None = None) -> None:
    path = marker or MARKER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"{version}\n"
        "# passbook schema version. Written by `make upgrade` AFTER the ledger\n"
        "# verified clean. Detection does not trust this file (see migrate.py);\n"
        "# deleting it costs a re-check, never a re-run of finished work.\n",
        encoding="utf-8",
    )


def pending(ctx: Any) -> list[tuple[Step, str]]:
    """Migrations with work to do, in the order they must run.

    Every step is asked, including ones the marker claims are done — the marker
    is a hint. A step whose `pending()` raises is reported as pending with the
    error, because "could not tell" must never read as "nothing to do".
    """
    out: list[tuple[Step, str]] = []
    for step in all_migrations():
        try:
            reason = step.pending(ctx)
        except Exception as exc:  # noqa: BLE001 — an unknown state is a pending state
            reason = f"could not determine ({exc.__class__.__name__}: {exc})"
        if reason:
            out.append((step, reason))
    return out
