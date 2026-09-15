"""Adding a bank without writing Python. SPEC §27, §49."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

import yaml

from flask import jsonify, request
from werkzeug.utils import secure_filename

from ...config import (
    BUILTIN_BANKS,
    SUPPORTED_BANKS,
    mask_account,
)
from ...loaders import profiles, read_grid, sniff
from ...loaders._table import CORE_COLS, REQUIRED_COLS, _all_aliases
from ...loaders._table import norm as COL_ALIASES_NORM
from ...loaders.pdf import PdfPasswordRequired, PdfPasswordWrong
from .. import auth as A

from ._base import (
    MAX_UPLOAD_BYTES,
    _fail,
    _money,
    api,
    log,
)
from ._scope import (
    BANDED_PREVIEW,
    SHAPE_LINES,
)


@api.post("/banks/try")
@A.login_required
def bank_try():
    """Parse the statement with a profile that has not been saved. SPEC §49.

    **Writes nothing.** No profile, no registry entry, no staged file, no push.

    It exists because the loop without it was: save the profile, go to another
    page, upload again, read a one-line rejection, come back, guess, repeat.
    Three rounds of that produced `row 10: transaction has no balance` — true,
    unactionable, and impossible for me to diagnose without reading a file the
    operator has told me never to open.

    So the diagnosis goes to the person who is allowed to see the data. On
    failure this returns **the banded rows themselves**: the operator looks at
    their own statement, in their own browser, and sees which column their
    balance actually landed in. That is one glance instead of a guessing game,
    and nothing leaves the machine that was not already on it.
    """
    upload = request.files.get("statement")
    if upload is None or not upload.filename:
        return _fail("No file chosen.")

    def _json(name: str) -> dict:
        try:
            loaded = json.loads(request.form.get(name) or "{}")
            return loaded if isinstance(loaded, dict) else {}
        except ValueError:
            return {}

    columns = {str(k).strip(): str(v).strip() for k, v in _json("columns").items() if str(k).strip()}
    metadata = {str(k).strip(): str(v).strip() for k, v in _json("metadata").items() if str(k).strip()}
    derive = (request.form.get("deriveTxnId") or "").lower() in {"1", "true", "yes"}
    try:
        dates = [str(d).strip() for d in json.loads(request.form.get("dates") or "[]") if str(d).strip()]
    except ValueError:
        dates = []
    if not columns:
        return _fail("Name the columns first.", "invalid", 422)

    scratch = Path(tempfile.mkdtemp(prefix="try-"))
    path = scratch / (secure_filename(upload.filename) or "statement")
    try:
        upload.save(path)
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            return _fail("That file is far too large to be a statement.", "too_large", 413)
        password = (request.form.get("password") or "").strip() or None
        try:
            return jsonify(_try_profile(path, password, columns, metadata, derive, dates))
        except PdfPasswordWrong as exc:
            return jsonify({"error": str(exc), "code": "pdf_password_wrong"}), 422
        except PdfPasswordRequired as exc:
            return jsonify({"error": str(exc), "code": "pdf_password"}), 422
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _try_profile(path, password, columns, metadata, derive, dates) -> dict:
    """Band the file with this mapping and say what happened. SPEC §49."""
    from ...loaders import _table, sniff
    from ...loaders._table import from_rows, norm
    from ...loaders.pdf import _decrypt, _lines
    from ...loaders.pdf_table import _learn_columns, _split, describe, find_header, to_grid
    from ...loaders.pdf_table import shape as pdf_shape
    from ...validate import BalanceBreak, check_continuity

    wanted = {norm(k): v for k, v in columns.items()}
    order = [f for f in ("date", "txn_id", "narration", "debit", "credit", "balance")
             if f in set(wanted.values()) or f == "narration"]
    if derive and "txn_id" in order:
        order.remove("txn_id")

    out: dict = {"container": sniff(path), "order": order}

    if out["container"] == "pdf":
        import pdfplumber

        with pdfplumber.open(_decrypt(path, password)) as document:
            lines = [line for page in document.pages for line in _lines(page)]
        header = find_header(lines, wanted)
        if header is None:
            out["error"] = (
                "None of those column names were found together on one line. Check "
                "the spelling against the table above — the words have to match, "
                "though case, spaces and punctuation do not."
            )
            return out
        at, cols, _headings = header
        split = _split(cols)
        text_edges, money_edges = _learn_columns(lines[at + 1 :], cols, split)
        out["headerLine"] = at
        # SPEC §50. The page as geometry and shapes — shareable, and useless to
        # anyone. `09-05-2026` becomes `92-92-94`; a payee becomes `A5`. Enough
        # to debug every banding failure this module has had, and it carries no
        # name, no payee, no account number and no amount.
        out["shapes"] = [
            f"{entry['line']:>3}: "
            + "  ".join(f"{w['shape']}@{w['x0']}-{w['x1']}" for w in entry["words"])
            for entry in describe(lines, SHAPE_LINES)
        ]
        # `to_grid` writes one grid row per preamble line and then the header,
        # so the header sits at the same index and the data starts after it.
        data_from = at + 1
        # Where each column was found, so a misread is visible as a number
        # rather than as a wrong total. Coordinates are not data.
        out["columns"] = {f: [round(x0, 1), round(x1, 1)] for f, (x0, x1) in cols.items()}
        # Both sides: where each column's values really start, and where its
        # figures really end. §51 — a heading is not its column.
        out["edges"] = {
            f: round(x, 1) for f, x in {**text_edges, **money_edges}.items()
        }
        grid = to_grid(lines, wanted, order)
    else:
        from ...loaders import read_grid

        grid = read_grid(path, out["container"], password)
        try:
            data_from = _table._find_header(grid or [], derive)[0] + 1
        except Exception:
            data_from = 1

    if not grid:
        out["error"] = "Nothing could be read out of that file."
        return out

    # The banded rows, for the operator's own eyes. This is their statement on
    # their screen; the point is that they can see which column their balance
    # landed in.
    out["banded"] = [[str(cell) for cell in row] for row in grid[:BANDED_PREVIEW]]
    out["bandedFrom"] = 0

    # The date column's own values, so a format can be offered rather than
    # demanded. Taken from the grid this mapping produced, which is the only
    # place they are known to be dates.
    if "date" in order:
        column = order.index("date")
        # **Data rows only.** Sampling from the preamble too fed account numbers
        # and phone numbers to the format detector, which requires every sample
        # to parse and therefore returned nothing at all.
        samples = [
            row[column]
            for row in grid[data_from:]
            if len(row) > column and row[column].strip()
        ][:40]
        out["dateCandidates"] = _table.date_formats_that_parse(samples)
        out["dateSample"] = samples[0] if samples else ""

    labels = _table.META_LABELS
    try:
        _table.META_LABELS = {**labels, **{norm(k): v for k, v in metadata.items()}}
        # The proposed formats, not the registered ones: this profile has
        # deliberately not been saved (§49).
        _table.DATE_FORMATS_OVERRIDE = dates or out.get("dateCandidates") or []
        # And the columns under test. `from_rows` re-derives the mapping from
        # the header text in the grid, and those words are only aliases once
        # the profile is saved — which is exactly what Try-it has not done.
        _table.COL_ALIASES_OVERRIDE = wanted
        meta, txns = from_rows(grid, derive_txn_id=derive)
    except Exception as exc:
        out["error"] = str(exc)
        # `row 11: transaction has no balance` names a grid row, and the banded
        # table right below it is that same grid. Marking the row turns "find
        # the one it named" into "look at the highlighted line", which is the
        # difference between a diagnosis and a word search. §49.4.
        named = re.search(r"\brow (\d+)\b", str(exc))
        if named:
            row = int(named.group(1))
            out["failedRow"] = row
            # The failing row as SHAPES, one safe line to paste. §52.2.
            #
            # The banded table above it shows real content, which is right for
            # the operator and useless for asking anyone else — so the row that
            # actually failed is also rendered the §50 way. Asking "which column
            # shows a dash" and getting no answer twice is a sign the question
            # was the wrong shape, not that nobody was listening.
            if 0 <= row < len(grid):
                out["failedRowShapes"] = "  ".join(
                    f"{field}={pdf_shape(str(cell)) or '—'}"
                    for field, cell in zip(order, grid[row])
                )
            if row >= out.get("bandedFrom", 0) + BANDED_PREVIEW:
                # The row it named is past the window. Move the window rather
                # than showing fourteen rows that all parsed.
                start = max(0, row - BANDED_PREVIEW // 2)
                out["banded"] = [
                    [str(cell) for cell in r] for r in grid[start : start + BANDED_PREVIEW]
                ]
                out["bandedFrom"] = start
        return out
    finally:
        _table.META_LABELS = labels
        _table.DATE_FORMATS_OVERRIDE = None
        _table.COL_ALIASES_OVERRIDE = None

    out["rows"] = len(txns)
    out["account"] = mask_account(meta.account_number)
    out["period"] = [meta.period_from.isoformat(), meta.period_to.isoformat()]
    out["opening"] = _money(meta.opening_balance)
    out["closing"] = _money(meta.closing_balance)
    try:
        check_continuity(meta, txns)
        out["ok"] = True
    except BalanceBreak as exc:
        # Parsed but does not add up: a column is mapped wrongly. This is the
        # check doing its job, so it is reported as the finding it is.
        out["ok"] = False
        out["error"] = str(exc)
    return out


@api.post("/banks")
@A.login_required
def bank_profile_save():
    """Write `config/banks/<bank>.yaml` from the browser. SPEC §34.

    A profile is a description of a layout, not a program, which is the whole
    reason adding a bank does not need Python. Writing one from a terminal did
    still need a terminal, and that was the last step keeping a non-developer
    out of their own second bank.

    Validated by **loading it back**: a profile that cannot be parsed is worse
    than none, because the parser raises on it rather than falling back — which
    is correct (§27.2) and would leave every bank unreadable until someone
    found the file.
    """
    body = request.get_json(silent=True) or {}
    bank = re.sub(r"[^a-z0-9-]", "", str(body.get("bank") or "").strip().lower())
    if not bank:
        return _fail("Give the bank a short name — lowercase letters and digits.", "invalid", 422)
    if bank in BUILTIN_BANKS:
        return _fail(f"{bank!r} is built in; it needs no profile.", "invalid", 422)

    columns = {str(k).strip(): str(v).strip() for k, v in (body.get("columns") or {}).items() if str(k).strip()}
    unknown = sorted(set(columns.values()) - set(profiles.FIELDS))
    if unknown:
        # Named properly, and it says which side is which. This message used to
        # read `unknown field(s): ['Balance', 'Chq', 'Date', ...]` — the
        # operator's own column headings listed back at them, because the page
        # posted the map inverted (§41.2). If it ever happens again the message
        # should at least say what it was expecting.
        return _fail(
            f"{'This is not a field' if len(unknown) == 1 else 'These are not fields'}"
            f" passbook knows: {', '.join(unknown)}."
            f" The fields are {', '.join(profiles.FIELDS)}.",
            "invalid",
            422,
        )
    # SPEC §44. `txn_id` is the one field a bank may genuinely not print —
    # Union Bank's cheque-number column is blank for every UPI and NEFT row.
    # Opt-in per profile and never inferred: an id synthesised because somebody
    # forgot to map a column would silently change how every row is identified,
    # forever.
    derive = bool(body.get("deriveTxnId"))
    required = CORE_COLS if derive else REQUIRED_COLS
    missing = required - set(columns.values())
    if missing:
        return _fail(
            f"still missing: {', '.join(sorted(missing))}. Every one is needed — "
            "without them the balance chain cannot be checked.",
            "invalid",
            422,
        )
    if derive and "txn_id" in set(columns.values()):
        return _fail(
            "You mapped a reference column and also ticked \u201cno reference "
            "number\u201d. Pick one — a real reference is always better.",
            "invalid",
            422,
        )

    metadata = {str(k).strip(): str(v).strip() for k, v in (body.get("metadata") or {}).items() if str(k).strip()}
    dates = [str(d).strip() for d in (body.get("dates") or []) if str(d).strip()]
    if dates:
        log.info("bank profile %s declares date format(s) %s", bank, dates)

    directory = profiles.PROFILES_DIR
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{bank}.yaml"
    document: dict = {"bank": bank, "columns": columns}
    if derive:
        document["derive_txn_id"] = True
    if metadata:
        document["metadata"] = metadata
    if dates:
        document["dates"] = dates

    header = (
        f"# {bank} statement layout. SPEC §27.\n"
        "# Written by the Add account page. Header matching ignores case, spaces\n"
        "# and punctuation, so only the words have to be right.\n"
    )
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(header + yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    tmp.replace(path)

    try:
        loaded = profiles.load_profiles()
    except profiles.ProfileError as exc:
        path.unlink(missing_ok=True)
        return _fail(f"That profile does not load: {exc}", "invalid", 422)

    log.info("bank profile written: %s (%d columns)", bank, len(columns))
    return jsonify(
        {"ok": True, "bank": bank, "path": str(path), "banks": [p["bank"] for p in loaded]}
    )


def _with_try_hint(exc) -> str:
    """A parse failure, plus where to go and see why. SPEC §49.3.

    `row 10: transaction has no balance` is true and unactionable, and this page
    cannot do better — it has one line and no room for evidence. The Add-a-bank
    page has the evidence: it prints the banded rows so the operator can see
    which column their balance actually landed in.

    An operator who hit this three times in a row was on the wrong page each
    time, and nothing on the page said there was a right one. Only added when a
    profile exists, because with no profile the remedy is to add the bank rather
    than to re-check one.
    """
    message = str(exc)
    try:
        from ...loaders.profiles import load_profiles

        if not load_profiles():
            return message
    except Exception:
        return message
    return (
        f"{message} — open **Accounts \u2192 Add a bank**, upload this same file and "
        "press **Try it**. It shows your statement laid out as the profile reads it, "
        "so you can see which column that figure landed in. Nothing is written there."
    )


def _metadata_preview(grid, header_row: int, proposed: dict[str, str]) -> dict[str, str]:
    """What the proposed labels resolve to in this file, masked. SPEC §45.

    Runs the **real** `_find_metadata`, with the operator's labels merged over
    the built-ins, rather than reimplementing the matching. Three strategies
    live in there (§44.5) and a second copy of them in the browser would drift
    from the one that decides whether an import succeeds.
    """
    from ...loaders import _table

    original = _table.META_LABELS
    try:
        _table.META_LABELS = {**original, **{COL_ALIASES_NORM(k): v for k, v in proposed.items()}}
        found = _table._find_metadata(grid, header_row)
    except Exception:  # a preview must never be the reason a page fails
        return {}
    finally:
        _table.META_LABELS = original

    out = {}
    for field, value in found.items():
        # The account number is the one being hunted for and the one that must
        # not be echoed (§11). Everything else here is already a label or a
        # branch code, neither of which is a credential.
        out[field] = mask_account(value) if field == "account_number" else value
    return out


@api.post("/banks/inspect")
@A.login_required
def bank_inspect():
    """`passbook inspect`, in the browser. SPEC §34.

    Adding a bank needs one thing the operator cannot get anywhere else: a look
    at their own file the way a parser sees it. That was CLI-only, which is a
    problem for the person who installed this from GitHub and has never opened
    a terminal.

    Reads the grid and **nothing else** — it does not stage the file, does not
    validate it, does not touch the registry and never writes. Safe on a
    statement from any bank, in any state, including one this cannot parse at
    all. That is the point: it is for the files that do NOT parse yet.
    """
    upload = request.files.get("statement")
    if upload is None or not upload.filename:
        return _fail("No file chosen.")

    scratch = Path(tempfile.mkdtemp(prefix="inspect-"))
    path = scratch / (secure_filename(upload.filename) or "statement")
    try:
        upload.save(path)
        if path.stat().st_size > MAX_UPLOAD_BYTES:
            return _fail("That file is far too large to be a statement.", "too_large", 413)

        kind = sniff(path)
        password = (request.form.get("password") or "").strip() or None
        try:
            grid = read_grid(path, kind, password)
        except PdfPasswordWrong as exc:
            # A DIFFERENT code from "needs a password", and that is the point.
            # Both used to be `pdf_password`, which means "show the password
            # box" — and the box was already showing, so a wrong password
            # produced no message and no change at all. §41.
            return jsonify({"error": str(exc), "code": "pdf_password_wrong"}), 422
        except PdfPasswordRequired as exc:
            return jsonify({"error": str(exc), "code": "pdf_password"}), 422
        except Exception as exc:  # a grid reader is allowed to fail on junk
            return _fail(f"Could not read it: {type(exc).__name__}", "rejected", 422)

        if grid is None:
            return _fail(
                f"No reader for {kind!r}. Upload the spreadsheet or PDF your bank gives you.",
                "rejected",
                422,
            )

        # SPEC §45. The page sends the metadata labels it is proposing, and gets
        # back what they actually resolve to — masked. Without this the operator
        # types a label, saves the profile, uploads on another page, and finds
        # out there whether it worked. The answer belongs where the question is.
        proposed: dict[str, str] = {}
        raw = (request.form.get("metadata") or "").strip()
        if raw:
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, dict):
                    proposed = {
                        str(k).strip(): str(v).strip()
                        for k, v in loaded.items()
                        if str(k).strip() and str(v).strip()
                    }
            except ValueError:
                proposed = {}

        aliases = _all_aliases()
        best_row, best = 0, {}
        for index, row in enumerate(grid[:50]):
            found: dict[str, int] = {}
            for column, cell in enumerate(row):
                field = COL_ALIASES_NORM(cell)
                mapped = aliases.get(field)
                if mapped and mapped not in found:
                    found[mapped] = column
            if len(found) > len(best):
                best_row, best = index, found

        return jsonify(
            {
                "container": kind,
                "rows": len(grid),
                # repr() per cell, so a single space is visibly a space. That
                # distinction is the likeliest silent bug in the whole project.
                "grid": [[repr(cell)[:40] for cell in row[:10]] for row in grid[:20]],
                "headerRow": best_row if best else None,
                "matched": {field: column for field, column in sorted(best.items())},
                "missing": sorted(REQUIRED_COLS - best.keys()),
                "required": sorted(REQUIRED_COLS),
                "banks": list(SUPPORTED_BANKS),
                # What the proposed labels find, masked. §11 holds here as
                # everywhere: the account number never crosses this boundary in
                # full, not even to the page that is trying to locate it.
                "metaFound": _metadata_preview(grid, best_row, proposed),
            }
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
