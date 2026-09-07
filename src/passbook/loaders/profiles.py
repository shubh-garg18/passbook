"""Bank profiles: adding a bank without writing Python. SPEC §27.

The table core (`_table.py`) already worked by mapping *normalised header text*
to field names. It just did it from a constant, which meant a second bank needed
a code change — and a code change means either the operator writes Python or
they send someone their statement. Neither is acceptable: the whole point of
this document trail is that a person can add their own bank on their own machine
without either.

So the constant is now a default and `config/banks/*.yaml` extends it. A profile
is a description of a layout, not a program:

```yaml
# config/banks/sbi.yaml
bank: sbi
columns:
  "Txn Date": date
  "Ref No./Cheque No.": txn_id
  "Debit": debit
  "Credit": credit
  "Balance": balance
  "Description": narration
metadata:
  "Account Number": account_number
  "Account Name": account_name
dates: ["%d %b %Y", "%d/%m/%Y"]
```

Keys are matched **tolerantly** — case, spaces and punctuation are stripped
before comparison — so `Ref No./Cheque No.` also matches `REF NO / CHEQUE NO`.
That matters because banks change capitalisation between exports and nobody
should have to chase it.

`passbook inspect <file>` prints exactly what to put in each field, reading the
operator's own file on their own machine. Nothing leaves it.

**A profile cannot soften a check.** It says where the columns are; the balance
continuity invariant, the Decimal handling and the duplicate-id check are the
same code for every bank. A layout that parses but does not chain is a wrong
profile, and it will say so.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from ..yamlfile import read_yaml

log = logging.getLogger(__name__)

PROFILES_DIR = Path("config/banks")

#: Profiles that ship with passbook. SPEC §55.
#:
#: Inside the package rather than in `config/`, for two reasons: they travel
#: with an install, and they are **not** the operator's to edit — a change here
#: would be lost on the next pull, whereas `config/banks/` is theirs and
#: persists. The operator's own always wins on a name clash, so a shipped
#: profile is a default and never a constraint.
BUILTIN_DIR = Path(__file__).resolve().parent.parent / "banks"

# Fields the table core needs. A profile may name any of them; anything it does
# not name falls back to the built-in Canara aliases.
FIELDS = ("date", "txn_id", "debit", "credit", "balance", "narration", "amount")
META_FIELDS = ("account_number", "customer_id", "account_name", "branch_code", "ifsc")


class ProfileError(ValueError):
    """A profile file is unusable. Never guessed around."""


def _norm(text: str) -> str:
    """Same normalisation the header matcher uses: alphanumerics, lowercased."""
    import re

    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def _column_ranges(path: Path, raw, columns: dict) -> dict[str, tuple[float, float]]:
    """`columns_at:` — where each column sits, in points. SPEC §99.

    **Some banks do not print their headings as text.** SBI's transaction table
    heads five of its six columns with a background graphic; only `Balance` is a
    word. `find_header` needs four matched names before it will call a line a
    header, so no amount of naming the columns can ever find it — the words are
    not in the file.

    So a profile may state the geometry instead. `columns_at` maps a field to
    the `[x0, x1]` its column occupies, measured off the operator's own page
    with `scripts/probe.py`, and a word belongs to the column whose range holds
    its **centre**.

    Centre, not edge, and that is the second thing this buys. `pdf_table` bands
    an inferred column by its right edge because money is right-aligned, which
    is how a statement is typeset — and SBI **centres** its figures: the same
    column ends at 379.7 for `250.00` and 383.1 for `1,000.00`, and both are
    centred on 367.5. A declared range does not care which, because it is not
    inferring anything.

    Validated hard, because every one of these is silent when wrong:

    * a field nobody knows about is a typo, not a column;
    * a range that is not two numbers left to right is unusable;
    * two ranges over the same point make placement arbitrary;
    * and a column named in `columns:` with no range would simply never be
      filled — the header would parse and the rows would come out empty.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProfileError(f"{path}: `columns_at` must be a mapping of field -> [x0, x1]")

    out: dict[str, tuple[float, float]] = {}
    for field, span in raw.items():
        field = str(field)
        if field not in FIELDS:
            raise ProfileError(
                f"{path}: `columns_at` names {field!r}, which is not a field; "
                f"valid fields are {list(FIELDS)}"
            )
        try:
            x0, x1 = (float(v) for v in span)
        except (TypeError, ValueError) as exc:
            raise ProfileError(
                f"{path}: `columns_at: {field}` must be two numbers [x0, x1]"
            ) from exc
        if not x0 < x1:
            raise ProfileError(f"{path}: `columns_at: {field}` runs {x0} to {x1}, backwards")
        out[field] = (x0, x1)

    ordered = sorted(out.items(), key=lambda pair: pair[1])
    for (left, (_a, a1)), (right, (b0, _b)) in zip(ordered, ordered[1:]):
        if b0 < a1:
            raise ProfileError(
                f"{path}: `columns_at` ranges for {left!r} and {right!r} overlap. "
                f"A word is placed by the range its centre falls in, so an "
                f"overlap decides nothing."
            )

    unplaced = {str(v) for v in columns.values()} - out.keys()
    if unplaced:
        raise ProfileError(
            f"{path}: `columns_at` is declared but says nothing about "
            f"{sorted(unplaced)}. A column with no range is never filled, and "
            f"the rows come out empty rather than wrong."
        )
    return out


def load_profiles(directory: Path | None = None) -> list[dict]:
    """Every profile passbook knows: shipped first, then the operator's. §55.

    The operator's own **wins on a name clash**, and that ordering is the whole
    contract: a shipped profile is what a fresh install can read out of the box,
    and the moment a bank re-skins its export the operator fixes it once on
    *Add a bank* and their version takes over without anyone editing the
    package.

    `directory` overrides the operator's half only — tests point it at a tmpdir
    and still exercise the shipped ones, which is what should happen.
    """
    local = directory or PROFILES_DIR
    out: list[dict] = []
    seen: dict[str, int] = {}

    for source, builtin in ((BUILTIN_DIR, True), (local, False)):
        for profile in _read_dir(source, builtin=builtin):
            if profile["bank"] in seen:
                # Same name from both halves: the operator's replaces the
                # shipped one outright rather than merging into it. A merge
                # would leave a column they deliberately removed still mapped.
                out[seen[profile["bank"]]] = profile
                log.info("bank profile %s overridden by %s", profile["bank"], source)
                continue
            seen[profile["bank"]] = len(out)
            out.append(profile)
    return out


def _read_dir(directory: Path, *, builtin: bool) -> list[dict]:
    """Every profile in one directory, or none. Missing is not an error."""
    if not directory.is_dir():
        return []

    out = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            data = read_yaml(path)
        except yaml.YAMLError as exc:
            raise ProfileError(f"{path} is not readable YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ProfileError(f"{path} should hold a mapping")

        bank = str(data.get("bank") or path.stem).strip().lower()
        columns = data.get("columns") or {}
        if not isinstance(columns, dict):
            raise ProfileError(f"{path}: `columns` must be a mapping of header -> field")
        unknown = {str(v) for v in columns.values()} - set(FIELDS)
        if unknown:
            raise ProfileError(
                f"{path}: unknown column field(s) {sorted(unknown)}; "
                f"valid fields are {list(FIELDS)}"
            )
        metadata = data.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ProfileError(f"{path}: `metadata` must be a mapping of label -> field")
        columns_at = _column_ranges(path, data.get("columns_at"), columns)

        out.append(
            {
                "bank": bank,
                "path": path,
                "columns": {_norm(k): str(v) for k, v in columns.items()},
                #: SPEC §99. Where the columns ARE, for a bank whose headings
                #: are not text. Empty for every bank that prints its own
                #: headings, which is the ordinary case.
                "columns_at": columns_at,
                "metadata": {_norm(k): str(v) for k, v in metadata.items()},
                "dates": [str(f) for f in (data.get("dates") or [])],
                # SPEC §44. Some banks print no per-row reference at all —
                # Union Bank's export has a cheque-number column that is blank
                # for every UPI and NEFT row, which is most of them. Declared
                # per profile and never inferred: an id synthesised because
                # somebody forgot to map a column would be a silent, permanent
                # change to how every row is identified.
                "derive_txn_id": bool(data.get("derive_txn_id")),
                #: Shipped with passbook rather than written here. The UI says
                #: so, because "where did this bank come from" is a fair
                #: question when nobody on this machine added it.
                "builtin": builtin,
            }
        )
        log.info(
            "bank profile %s: %d column(s)%s", bank, len(columns), " (built in)" if builtin else ""
        )
    return out


def derives_txn_id(directory: Path | None = None) -> bool:
    """Whether any profile says its bank prints no per-row reference. §44."""
    return any(p["derive_txn_id"] for p in load_profiles(directory))


def column_aliases(directory: Path | None = None) -> dict[str, str]:
    """Every profile's column mapping, merged over the built-in one."""
    merged: dict[str, str] = {}
    for profile in load_profiles(directory):
        merged.update(profile["columns"])
    return merged


def metadata_labels(directory: Path | None = None) -> dict[str, str]:
    merged: dict[str, str] = {}
    for profile in load_profiles(directory):
        merged.update(profile["metadata"])
    return merged


def date_formats(directory: Path | None = None) -> list[str]:
    """Extra `strptime` formats to try, in the order profiles declare them."""
    out: list[str] = []
    for profile in load_profiles(directory):
        for fmt in profile["dates"]:
            if fmt not in out:
                out.append(fmt)
    return out


def known_banks(directory: Path | None = None) -> list[str]:
    return sorted({p["bank"] for p in load_profiles(directory)})
