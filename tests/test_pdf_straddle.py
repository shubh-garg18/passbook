"""A description centred on its row, half of it above the row. SPEC §96.

Canara wraps downward only, so appending every continuation to `grid[-1]` was
correct there and this could not appear on the reference bank. On a bank that
centres the block, the first line lands on the PREVIOUS transaction — and the
symptom is not a crash but silence: every payee unparsed, and a `CR`
description sitting on a withdrawal.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "fixtures"))

from straddled_narration import PROFILE, lines  # noqa: E402

from passbook.loaders.pdf_table import to_grid  # noqa: E402

ORDER = ["date", "narration", "debit", "credit", "balance"]


def _grid():
    grid = to_grid(lines(), PROFILE["columns"], ORDER)
    assert grid is not None, "the header did not match"
    # Row 0 is the header echo; the transactions follow.
    return grid[1:]


def test_both_halves_land_on_the_row_they_are_centred_on():
    rows = _grid()
    by_date = {row[0]: row[1] for row in rows if row[0]}
    assert "FIRSTHALF SECONDHALF" in by_date["06-04-2024"], by_date
    # And neither neighbour collected a fragment.
    assert "FIRSTHALF" not in by_date["01-04-2024"]
    assert "SECONDHALF" not in by_date["30-04-2024"]


def test_the_row_above_does_not_absorb_the_line_above_the_next_row():
    """The precise failure: appending to `grid[-1]` gave row 1 the first half.

    Stated as its own test because it is the regression, and because a test
    that only checks the happy row would still pass if the fix simply dropped
    the continuation instead of moving it.
    """
    rows = _grid()
    first = next(row for row in rows if row[0] == "01-04-2024")
    assert "FIRSTHALF" not in first[1]


def test_reattachment_changes_the_narration_column_and_nothing_else():
    """Narration moves; nothing else may.

    Asserted as a DIFFERENCE against the same page with its wrapped lines
    removed, rather than against hardcoded cells. That way it cannot pass or
    fail for reasons belonging to the money banding, which `test_pdf_centred`
    already covers — and it states the actual invariant, which is that
    reattachment is confined to one column.

    It needs saying because a description landing on the wrong row is invisible
    to the balance chain: §6.6 checks amounts, and every amount here is right
    either way. That is exactly why the bug survived a clean continuity check.
    """
    with_wraps = _grid()
    without = to_grid(
        [line for line in lines() if len(line) > 1 or line[0]["x0"] < 100],
        PROFILE["columns"],
        ORDER,
    )
    assert without is not None
    without = without[1:]
    assert len(with_wraps) == len(without), "reattachment created or dropped a row"

    for a, b in zip(with_wraps, without):
        for column, (left, right) in enumerate(zip(a, b)):
            if column == ORDER.index("narration"):
                continue
            assert left == right, f"{ORDER[column]} changed: {right!r} -> {left!r}"
