"""Parse each archived statement once, ever. SPEC §110.

**Measured, per §6k.** Parsing everything under `archive/` on this machine:

    51ms    93 rows    28 KB   Acnt_stmt__07052026_07082026.xls
     9ms    32 rows    13 KB   Acnt_stmt__15072026_08082026.xls
     8ms    46 rows    16 KB   Acnt_stmt__25072026_25082026.xls
  1416ms    32 rows  1680 KB   a 1.7 MB PDF
  ----
  1484ms  paid on every cold start, and it only grows

`archived_statements` is read by `/overview`, `/analysis`, `/transactions`,
`/payees`, `/status`, `verify-ledger` and the reminder. §101 cached it in
memory, which fixed the *repeat* cost and left the first call of every process
paying the whole thing — 1.5 seconds before the first page can render, growing
by one statement every week.

## Why a file cache and not a database

**An archived statement never changes.** A file lands in `archive/` only after a
fully successful push (§7.3), and nothing rewrites it. So this is not a cache in
the hard sense — there is no staleness to manage, no invalidation to get wrong
and no coherence problem. It is a memo of a pure function.

That rules out the things it looks like it needs:

* **a database** would add a schema, migrations and a second source of truth for
  data that already has one. `archive/` is what `verify-ledger` compares the
  ledger against (§20); a table claiming to hold the same rows would be a place
  for the two to disagree;
* **a cache service** would add a network hop and a running process to avoid a
  local CPU cost, on a laptop that stops when Windows sleeps (D7).

What is actually needed is: parse this exact file once and remember the answer.
A file keyed on the file's own content hash is that, and nothing more.

## What it holds, and where

Real statements — payees, amounts, the account number. It is therefore treated
exactly like `archive/`: gitignored, and covered by the same rule that keeps
statement data out of the repository (non-negotiable 6).

Written atomically, and **read defensively**: a truncated or hand-edited entry
falls back to parsing the file, because a cache that can break an import is
worse than no cache. It is a memo, and a memo may always be forgotten.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from pathlib import Path

from .models import StatementMeta, Transaction

log = logging.getLogger(__name__)

#: Beside `archive/`, not inside it: everything in `archive/` is a statement,
#: and `archived_statements` globs it. A cache entry landing in there would be
#: read back as a statement that failed to parse.
CACHE_DIR = Path(".cache/statements")

#: Bumped when a LOADER changes what it produces — a column mapped differently,
#: a row banded somewhere else. An entry written by an older passbook is not
#: wrong so much as stale in a way nothing else can see.
#:
#: It does **not** need bumping when a narration grammar or a rule changes:
#: this memo stops at the file parse, and `parse_statement` still enriches and
#: validates on every call. §106 gave thirteen rows a payee they did not have
#: the day before, and that took effect with no bump and no cache to clear.
VERSION = 1


def _key(path: Path) -> str | None:
    """This exact file's identity: its content, hashed. None if unreadable.

    The content and not the path, so a statement re-archived under a new name —
    or the same name in a different folder after §21.6 — is recognised as the
    file it already is.
    """
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None
    return f"{VERSION}-{digest[:32]}"


def load(path: Path) -> tuple[StatementMeta, list[Transaction]] | None:
    """The remembered parse of this file, or None to parse it properly."""
    key = _key(path)
    if key is None:
        return None
    entry = CACHE_DIR / f"{key}.json"
    try:
        raw = json.loads(entry.read_text(encoding="utf-8"))
        meta = StatementMeta.model_validate(raw["meta"])
        rows = [Transaction.model_validate(t) for t in raw["transactions"]]
    except FileNotFoundError:
        return None
    except Exception as exc:
        # Truncated, hand-edited, or written by a shape this version does not
        # know. Parsing the file is always available and always correct, so a
        # bad entry costs time and never an answer.
        log.info("ignoring an unreadable parse memo for %s: %s", path.name, exc)
        entry.unlink(missing_ok=True)
        return None
    return meta, rows


def store(path: Path, meta: StatementMeta, transactions: list[Transaction]) -> None:
    """Remember this parse. Failing to write is not an error worth raising."""
    key = _key(path)
    if key is None:
        return
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        body = json.dumps(
            {
                "meta": json.loads(meta.model_dump_json()),
                "transactions": [json.loads(t.model_dump_json()) for t in transactions],
            }
        )
        # Atomic, and owner-only: this holds payees and amounts (§11). Written
        # to a temp file in the same directory so the rename cannot cross a
        # filesystem, and chmod BEFORE the rename — a window on a world-readable
        # file is a window, however short.
        handle, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, CACHE_DIR / f"{key}.json")
    except Exception as exc:  # pragma: no cover — a full or read-only disk
        log.info("could not write a parse memo for %s: %s", path.name, exc)


def forget_everything() -> None:
    """Drop every entry. For tests, and for `make clean`."""
    if not CACHE_DIR.is_dir():
        return
    for entry in CACHE_DIR.glob("*.json"):
        entry.unlink(missing_ok=True)


#: How many memos for files no longer in the archive to keep. SPEC §117.1.
#:
#: Not zero, because a **staged** statement has a memo and is not in the archive
#: by definition — it has not been pushed. Uploading, previewing and confirming
#: re-read the same file, and pruning between them would cost a re-parse of the
#: one file the operator is actively waiting on.
#:
#: Not unbounded either, which is what it was: one memo is written per uploaded
#: file, including the ones that were rejected and re-uploaded, and nothing ever
#: removed them. Measured on this machine at 10-36 KB each — slow to matter and
#: certain to.
SPARE = 8


def prune(keep: set[str]) -> int:
    """Drop memos that are neither in `keep` nor among the newest `SPARE`.

    `keep` is the set of content hashes the archive currently holds, which the
    index sync has already computed — so this costs a directory listing.

    Returns how many were removed. Failing to prune is never an error: the cost
    of a memo that outlives its file is disk, and the cost of raising here would
    be a page.
    """
    try:
        entries = sorted(
            CACHE_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
    except OSError:
        return 0

    # **Counted among the strays only.** Counting archived memos toward the
    # budget makes it shrink as the archive grows: at a hundred statements the
    # newest `SPARE` entries are all archived ones, so zero strays are spared
    # and the staged file's memo — the one the operator is waiting on — is the
    # first thing pruned. Caught by the test for exactly that case.
    dropped = 0
    strays = 0
    for entry in entries:
        if entry.stem in keep:
            continue
        strays += 1
        if strays <= SPARE:
            continue
        try:
            entry.unlink()
            dropped += 1
        except OSError:  # pragma: no cover — a read-only cache directory
            pass
    if dropped:
        log.info("dropped %d parse memo(s) whose statement is no longer archived", dropped)
    return dropped

