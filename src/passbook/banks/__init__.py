"""The bank registry — the seam a second bank goes through. SPEC §22.5.

Everything that is *about Canara* rather than *about statements* lives in one
`Bank` value: what its header columns are called, how it writes a date, what its
sentinel rows say, which narration grammars it speaks, and how its PDF is
locked. `loaders/` and `narration.py` are then bank-agnostic machinery that
reads those fields.

Adding a bank is therefore a new file in this package and a `register()` call.
No edits to the loaders, the validator, the pusher or the UI. `docs/adding-a-bank.md`
walks through it with the checklist and the invariant a new dialect must satisfy.

**Why a value object rather than a base class.** Subclassing invites a new bank
to override `from_rows` and end up with its own copy of the balance-continuity
invariant, which is the one piece of this project that must never fork. A
dataclass of data and small pure functions cannot be overridden into a second
ledger.

**Detection is by content, not by filename or by config.** `detect(rows)` is
asked of every registered bank and exactly one must answer. A statement moved by
hand into the wrong folder must not change how it is read — the same rule §21.6
applies to which *account* a statement belongs to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, time
from typing import Callable, Mapping, Protocol

from ..loaders._table import ParseError

Rows = list[list[str]]


class Matcher(Protocol):
    """A narration grammar. Returns the fields it recognised, or None.

    Never raises: an unrecognised narration falls through to `OTHER` with the
    raw string preserved. A bank whose matcher throws on an unexpected shape
    would turn one odd row into a failed sync.
    """

    __name__: str

    def __call__(self, raw: str) -> dict | None: ...


@dataclass(frozen=True)
class Bank:
    """One bank's statement dialect. Data and pure functions only."""

    slug: str
    """Registry key and `external_id` prefix component. `[a-z0-9-]+`."""

    name: str
    """What a human sees: "Canara Bank"."""

    # ── the grid (§6.3) ──────────────────────────────────────────────────────
    column_aliases: Mapping[str, str]
    """Normalised header text -> canonical field. Include the bank's own
    misspellings verbatim: Canara ships `Trasnaction ID` and matching it as
    written is the difference between a parser and a wish."""

    required_columns: frozenset[str]
    """Canonical fields without which the grid is not a statement."""

    metadata_labels: Mapping[str, str]
    """Normalised label text -> `StatementMeta` field."""

    metadata_label_columns: tuple[int, ...] = (0, 3)
    """Which columns carry labels in the preamble block. Canara uses two pairs
    side by side."""

    opening_label: str = "openingbalance"
    closing_label: str = "closingbalance"
    """Normalised sentinel row labels. Rows between them are the transactions."""

    period_pattern: re.Pattern[str] = re.compile(
        r"from\s+(\S+)\s+to\s+(\S+)", re.IGNORECASE
    )
    """Finds the statement period in the preamble. Two capture groups, each
    parseable by `parse_date`."""

    parse_date: Callable[[str], date] = None  # type: ignore[assignment]
    """Text -> date. Locale-independent, always: `%b` differs between machines
    and half these exports are uppercase."""

    # ── narration (§6.5) ─────────────────────────────────────────────────────
    narration_matchers: tuple[Matcher, ...] = ()
    """Tried in order, first hit wins."""

    strip_trailing_timestamp: Callable[[str], str] = lambda raw: raw
    """Remove a trailing timestamp before tokenising. For Canara this is the
    single most important line in the parser: the timestamp contains slashes."""

    extract_time: Callable[[str], time | None] = lambda raw: None
    """Recover the time of day, if the narration carries one. `None` where it
    does not — never midnight, which invents a transaction that did not happen."""

    # ── the PDF fallback (§6.8) ──────────────────────────────────────────────
    pdf_password_hint: str = ""
    """One sentence naming what the password is, printed when a PDF is refused.
    Empty when this bank's PDF is not encrypted."""

    # ── detection ────────────────────────────────────────────────────────────
    detect: Callable[[Rows], bool] = None  # type: ignore[assignment]
    """Does this grid belong to this bank? Read a marker out of the preamble —
    the bank's own name, or a header spelling only it uses."""

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9-]+", self.slug):
            # The slug becomes part of every `external_id` (§21.1), so it has to
            # survive a regex separation from the bank's own transaction id.
            raise ValueError(f"bank slug {self.slug!r} must match [a-z0-9-]+")
        if self.parse_date is None or self.detect is None:
            raise ValueError(f"bank {self.slug!r} needs parse_date and detect")


class UnknownBank(ParseError):
    """No registered bank claims this statement, or this slug.

    A `ParseError` on purpose. Every front end already turns one into a 422
    "we could not read this file" that deletes the staged upload; a bare
    LookupError would surface as a 500 and leave the file in `inbox/`, where the
    next `make sync` would find it.
    """


_REGISTRY: dict[str, Bank] = {}


def register(bank: Bank) -> Bank:
    """Add a bank. Called at import time by each module in this package."""
    if bank.slug in _REGISTRY and _REGISTRY[bank.slug] is not bank:
        raise ValueError(f"two banks registered as {bank.slug!r}")
    _REGISTRY[bank.slug] = bank
    return bank


def registered() -> list[Bank]:
    """Every bank, in slug order. What an error message lists."""
    _load()
    return [_REGISTRY[slug] for slug in sorted(_REGISTRY)]


def slugs() -> list[str]:
    return [bank.slug for bank in registered()]


def get(slug: str) -> Bank:
    """Look up by slug — what `config/accounts.yaml`'s `bank:` field resolves.

    Refused loudly rather than defaulted: an account registered against a bank
    with no loader would parse its statements with the wrong dialect and land
    plausible, wrong rows in the ledger.
    """
    _load()
    try:
        return _REGISTRY[slug]
    except KeyError:
        raise UnknownBank(
            f"no loader for bank {slug!r}. Registered: {', '.join(sorted(_REGISTRY)) or 'none'}. "
            "See docs/adding-a-bank.md."
        ) from None


def detect(rows: Rows) -> Bank:
    """Which bank wrote this grid.

    Exactly one must claim it. Two claims is a bug in a `detect` function, and
    guessing between them would silently attach a statement to the wrong
    dialect — so it raises instead.
    """
    _load()
    claims = [bank for bank in registered() if bank.detect(rows)]
    if len(claims) == 1:
        return claims[0]
    if not claims:
        raise UnknownBank(
            "this file does not look like a statement from any bank passbook knows: "
            f"{', '.join(slugs())}. If it is from a new bank, "
            "docs/adding-a-bank.md is the 200-line version of this problem."
        )
    raise UnknownBank(
        f"{len(claims)} banks claim this statement ({', '.join(b.slug for b in claims)}). "
        "Their detect() functions overlap; narrow them before trusting either."
    )


_loaded = False


def _load() -> None:
    """Import every bank module once, so registration is a side effect of use."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    import importlib
    import pkgutil

    for info in pkgutil.iter_modules(__path__):
        if not info.name.startswith("_"):
            importlib.import_module(f"{__name__}.{info.name}")
