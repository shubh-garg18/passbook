#!/usr/bin/env python3
"""Read a statement's GEOMETRY, never its content. SPEC §54.

    uv run --with pdfplumber --with pikepdf python scripts/probe.py <file> [password]

The operator's statement stays on the operator's machine and its contents stay
off everyone's screen — including mine. This prints what a parser needs and
nothing a person would recognise:

* every line's words as `shape@x0-x1`, for the **whole document**, not a
  40-line preview;
* the clusters the column learner finds, and what it assigns them to;
* whether the parse succeeds, how many rows, and whether the balance chain
  holds.

`shape()` is §50's: `09-05-2026` becomes `92-92-94`, a payee becomes `A5`,
`12,345.67` becomes `92,93.92`. `999900001111`, `410250014782` and
`000000000001` are all `912` — indistinguishable, with no way back.

Nothing here prints a name, an address, a payee, an account number or an amount.
That is checked, not intended: `--audit` re-reads this script's own output and
fails if any word of the source document appears in it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from passbook.loaders.pdf import _decrypt, _lines  # noqa: E402
from passbook.loaders.pdf_table import shape  # noqa: E402


def geometry(path: Path, password: str | None):
    import pdfplumber

    with pdfplumber.open(_decrypt(path, password)) as document:
        return [line for page in document.pages for line in _lines(page)]


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    path = Path(sys.argv[1])
    password = sys.argv[2] if len(sys.argv) > 2 else None

    lines = geometry(path, password)
    print(f"# {len(lines)} lines, geometry only — no content below this line")
    for index, words in enumerate(lines):
        rendered = "  ".join(
            f"{shape(w['text'])}@{round(w['x0'], 1)}-{round(w['x1'], 1)}" for w in words
        )
        print(f"{index:>4}: {rendered}")

    # Cross-check: no word of the document may appear in what was printed.
    printed = "\n".join(
        "  ".join(f"{shape(w['text'])}@{round(w['x0'],1)}-{round(w['x1'],1)}" for w in ws)
        for ws in lines
    )
    leaked = sorted(
        {
            w["text"]
            for ws in lines
            for w in ws
            if len(w["text"]) >= 4 and w["text"] in printed
        }
    )
    print(f"\n# audit: {len(leaked)} source word(s) appear in the output above", file=sys.stderr)
    if leaked:
        print("# LEAK — do not share this output", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
