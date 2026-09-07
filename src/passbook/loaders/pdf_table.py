"""Any bank's PDF, as a grid. SPEC §44.

**Most Indian banks hand out a password-protected PDF and nothing else.** That
makes this the main path, not a fallback: `config/banks/*.yaml` could describe
any bank's columns and none of it reached a PDF, because `pdf.py` is a
reconstruction of *Canara's* line-wrapping algorithm and reads nothing else.

A spreadsheet arrives as cells. A PDF arrives as words at coordinates, and the
columns have to be recovered from where the words sit. That recovery is what
this module is: read the header line, take the x-range of each column name, and
band every word below it.

## The rule that makes it work, and the one that breaks it

Money columns are **right-aligned** and text columns are **left-aligned**. That
is not a guess about this bank, it is how a statement is typeset — a wide figure
and a narrow one in the same column share a right edge and share nothing on the
left.

So: a word is placed by where it **ends** if it lands among the money columns,
and by where it **starts** if it does not. `pdf.py` learned this the hard way
and the comment there is worth repeating, because a midpoint rule looks correct
and is not:

    a long narration token starting inside its own column but ending past the
    midpoint was counted as narration *and* as a withdrawal

Measured on the fixture: the trailing UPI timestamp `01:51:33` sits at
x 236.8-275.7, whose midpoint is past the Particulars/Withdrawals midpoint. By
start, it is narration. By midpoint, it is money.

## What it refuses to guess

**A word in a money column that is not money is not money.** It goes to the
narration cell instead. That is what keeps `Opening Balance` — a label that sits
under the Withdrawals column on this bank's paper — from being handed to
`parse_amount`, and it is why the sentinel rows survive the trip.

**Nothing here softens a check.** The grid this produces goes through the same
`from_rows` as a spreadsheet, so the balance chain (§6.6) is what says whether
the bands were right. A column mapped one place left will not add up, loudly,
with the row index.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from itertools import combinations

from ._table import ParseError, norm, parse_amount

log = logging.getLogger(__name__)

#: Fields whose columns are right-aligned on a printed statement.
MONEY_FIELDS = frozenset({"debit", "credit", "balance", "amount"})

#: Clear of the widest text line, and left of where any figure can start. Same
#: constant and same reasoning as `pdf.py`'s NARRATION_MARGIN.
MONEY_MARGIN = 12.0

#: A gap wider than this separates two cells on a preamble line. Measured: words
#: inside one phrase sit 2.7-2.8pt apart on the fixture, label and value 49pt.
PREAMBLE_GAP = 8.0

#: How many consecutive words a single column heading may be. "Withdrawal Amt"
#: is two; "Ref No. / Cheque No." is four.
MAX_HEADING_WORDS = 4

#: How far left of the leftmost mapped column a word may start and still belong
#: to it. Beyond this it belongs to a column nobody mapped, and is dropped.
UNMAPPED_MARGIN = 6.0

#: How far outside its heading a column's edge may sit before the pairing is
#: rejected as physically impossible. A heading is printed inside its own
#: column, so a left-aligned column's values start at or left of the heading's
#: left edge, and a right-aligned column's figures end at or right of the
#: heading's right edge. A few points of slack for the odd narrow heading.
INSIDE_SLACK = 4.0

#: How far a figure's right edge may sit from its column's, in points.
#:
#: This is the test for "is this word actually in a money column", and it exists
#: because being roughly over there is not enough. Measured on the fixture: real
#: amounts end within 1.0pt of the heading's right edge — `1,418.91` at 418.0
#: under `Withdrawals` ending 417.0, `8,581.09` at 573.0 under `Balance` ending
#: 573.0. The page footer `Page 1 of 6` sits at 503-539 and its digits end 34pt
#: short of any column, so under this rule they are not figures at all.
#:
#: The premise is that money columns are right-aligned, which is how a statement
#: is typeset. A bank that centres them would fail the balance chain loudly
#: rather than import something wrong.
MONEY_ALIGN_TOLERANCE = 10.0

#: Two figures further apart than this belong to different columns. Amounts in
#: one right-aligned column share an edge to within a point; the narrowest
#: column gap seen is 38pt.
CLUSTER_GAP = 12.0

#: How many figures a cluster needs before it counts as a column. A page footer
#: contributes one token per page; a real column contributes one per row.
MIN_CLUSTER = 3

#: How far ABOVE its own dated line a description block may begin, in points.
#:
#: A wrapped narration belongs to a transaction, and which one is decided by
#: vertical position — but "the row it is nearest to" is not the rule, because
#: a block is not centred on its row on every bank. Measured on two:
#:
#:     Union   block spans -5.5 to +5.5 of its row, rows 20pt apart
#:     SBI     block spans -7.1 to +30.1 of its row, rows 50pt apart
#:
#: SBI's last description line is 30pt below its own row and 20pt above the
#: next, so nearest-first hands it to the wrong transaction. What both banks
#: share is that a block starts a few points above its dated line and runs
#: down: so a continuation belongs to the **last row at or above it**, and this
#: is how much "at" is allowed to overshoot. Both first lines (7.1 and 5.5) fit
#: under it, and both next rows (20 and 50 away) stay clear of it.
BLOCK_OVERHANG = 8.0

#: What a bank prints in a money column that holds no figure.
#:
#: Not money, and not narration either. `_row` sends a non-figure in a money
#: column to the narration cell, which is what keeps an `Opening Balance` label
#: out of `parse_amount` — but a placeholder is not a label, and letting it
#: through prefixes every SBI payee with `- -`. Dropped, so an empty cell reads
#: as empty. SPEC §99.
MONEY_PLACEHOLDERS = frozenset({"-", "\u2013", "\u2014"})


def _group(words: list[dict], gap: float) -> list[list[dict]]:
    """Split a line into cells wherever the words are further apart than `gap`."""
    cells: list[list[dict]] = []
    for word in words:
        if cells and word["x0"] - cells[-1][-1]["x1"] <= gap:
            cells[-1].append(word)
        else:
            cells.append([word])
    return cells


def _text(words: list[dict]) -> str:
    return " ".join(w["text"] for w in words)


def find_header(
    lines: list[list[dict]], wanted: dict[str, str]
) -> tuple[int, dict[str, tuple[float, float]], dict[str, str]] | None:
    """The header line, the x-range of each column, and the heading text.

    The heading text is returned because the grid this module produces is read
    back by the same `_find_header` a spreadsheet goes through — so its header
    row has to carry **the bank's own words**, not passbook's field names. A
    grid headed `date, narration, debit` parses as a spreadsheet with no header
    at all.

    `wanted` is a profile's `{normalised heading: field}`. A heading may be
    several words, so this walks each line greedily trying the longest match
    first — otherwise `Withdrawal` would match and `Withdrawal Amt` never would.

    **The best-scoring line wins, not the first one over the bar.** Many bank
    PDFs open with a *details* table — name, address, account number, IFSC — and
    then start the transactions in a second table below it. A details line that
    happens to carry four of the mapped words would have been taken as the
    header, every band computed from the wrong place, and every transaction
    below it read as empty. The real header matches more of them, so scoring the
    whole page and taking the maximum picks it out; ties go to the first, which
    keeps single-table files behaving exactly as before.

    Returns None rather than raising: a caller may be trying several profiles,
    and "this is not that bank" is an ordinary answer.
    """
    best: tuple[int, dict, dict] | None = None
    for index, words in enumerate(lines):
        found: dict[str, tuple[float, float]] = {}
        headings: dict[str, str] = {}
        position = 0
        while position < len(words):
            for span in range(min(MAX_HEADING_WORDS, len(words) - position), 0, -1):
                chunk = words[position : position + span]
                field = wanted.get(norm(_text(chunk)))
                if field and field not in found:
                    found[field] = (chunk[0]["x0"], chunk[-1]["x1"])
                    headings[field] = _text(chunk)
                    position += span
                    break
            else:
                position += 1
        # Four is the same bar `_table._find_header` uses: enough to be a header
        # rather than a sentence that happens to contain one column name.
        if len(found) >= 4 and (best is None or len(found) > len(best[1])):
            best = (index, found, headings)

    if best is None:
        return None
    log.info("pdf header at line %d: %s", best[0], sorted(best[1]))
    return best


#: A trailing marker Indian statements print on balances: `12,345.67 Cr`.
#: `Cr` is the ordinary case; `Dr` means the account is overdrawn.
_SUFFIX = {"cr": 1, "dr": -1}


def _amount_text(text: str) -> str | None:
    """The figure in this token, or None if it is not one.

    Handles the `Cr`/`Dr` suffix, which is near-universal on Indian statements
    and which `parse_amount` rightly refuses: `12,345.67 Cr` is not a number.
    `Dr` on a balance means overdrawn, so it comes back negative — and if that
    reading is ever wrong for some bank, the balance chain says so immediately
    and with a row index, which is exactly what it is for.
    """
    stripped = text.strip()
    if not stripped:
        return None
    sign = 1
    for suffix, direction in _SUFFIX.items():
        if stripped.lower().endswith(suffix) and len(stripped) > len(suffix):
            sign = direction
            stripped = stripped[: -len(suffix)].strip().rstrip(".").strip()
            break
    try:
        value = parse_amount(stripped)
    except ParseError:
        return None
    if value is None:
        return None
    return str(-value if sign < 0 else value)


def _cluster(values: list[float], *, popular_only: bool = False) -> list[float]:
    """Cluster nearby coordinates and return the median of each. §51.

    `popular_only` first discards positions that only a handful of words start
    at, and it is essential on the text side. SPEC §54.

    Single-linkage clustering chains: a run of positions each within
    `CLUSTER_GAP` of the next becomes one cluster however far it spans. Money
    columns are sparse — a statement has three or four x-positions where figures
    end — so it works there. **Narration does not.** A real statement's
    descriptions wrap and break at sixty different x-positions, each used by one
    or two words, and the whole left half of the page chains into a single
    cluster from 105 to 318. Measured on a real file: the date column at x 50
    and the serial column at x 35 disappeared into it, and `date` was then
    *estimated* at x 18.6 — off the page.

    A column start is a place where **many rows begin a word**. A position one
    word starts at is a word in the middle of a sentence. Counting first is what
    separates the two, and it is why the Canara fixture never showed this: its
    narrations are one long token, so there was nothing to chain.
    """
    if not values:
        return []
    if popular_only:
        from collections import Counter

        counts = Counter(round(v, 0) for v in values)
        values = [v for v in values if counts[round(v, 0)] >= MIN_CLUSTER]
        if not values:
            return []
    values = sorted(values)
    groups: list[list[float]] = [[values[0]]]
    for value in values[1:]:
        if value - groups[-1][-1] <= CLUSTER_GAP:
            groups[-1].append(value)
        else:
            groups.append([value])
    # Median, not mean: one mis-banded token should not drag an edge.
    return [group[len(group) // 2] for group in groups if len(group) >= MIN_CLUSTER]


def _spans(words: list[dict]) -> list[tuple[float, float]]:
    """Left-aligned columns as `(start, typical end)`, clustered on the start.

    The end comes back too because a **start alone cannot say which column a
    cluster is.** A statement with an unmapped serial-number column has a
    cluster at x 35 and the real Date column at x 50, and the Date heading sits
    at 63.5 — nearer to neither by much. What separates them is that the date
    *values* span 50 to 92 and therefore lie **under** their heading, while the
    serial numbers span 35 to 40 and lie beside it.

    Median end, not maximum: one long narration should not make its column look
    like it reaches the far edge of the page.
    """
    by_start: dict[float, list[float]] = defaultdict(list)
    starts = _cluster([w["x0"] for w in words], popular_only=True)
    if not starts:
        return []
    for word in words:
        nearest = min(starts, key=lambda s: abs(word["x0"] - s))
        if abs(word["x0"] - nearest) <= CLUSTER_GAP:
            by_start[nearest].append(word["x1"])
    out = []
    for start in starts:
        ends = sorted(by_start.get(start, [start]))
        out.append((start, ends[len(ends) // 2]))
    return out


def _overlaps(span: tuple[float, float], heading: tuple[float, float]) -> bool:
    """Whether a column's values sit under its heading, allowing a small miss."""
    return span[0] <= heading[1] + CLUSTER_GAP and span[1] >= heading[0] - CLUSTER_GAP


def _match(
    anchors: list[tuple[float, str]],
    found: list[float],
    *,
    right_aligned: bool,
    under: dict[float, tuple[float, float]] | None = None,
) -> dict[str, float] | None:
    """Line the observed columns up with the headings. `None` if it cannot.

    `anchors` are `(heading coordinate, field)` left to right; `found` are the
    coordinates the data actually clustered at. There may be fewer clusters than
    columns — a statement with two deposits in three months has a real Deposit
    column and not enough figures in it to cluster — so this is an alignment,
    not a pairing.

    **A heading sits inside its own column.** That is the constraint that makes
    this decidable, and it comes from typesetting rather than from taste: a
    left-aligned column's values cannot begin to the *right* of where its
    heading begins, and a right-aligned column's figures cannot end to the
    *left* of where its heading ends. Without it, a cluster from a neighbouring
    column can score better than the real one — measured, on a six-page
    statement: the Date column was learned at 105.0, which is where the
    *narration* starts, because that assignment's offsets happened to agree
    more closely. §53.

    Among the assignments that respect it, the winner is the one whose offsets
    are **most consistent**. Nearest-first is what a person does by eye and is
    wrong: with clusters at 395 and 560 against headings at 382, 454 and 542,
    nearest-first gives 395 to the first and then hands 560 to the *second*,
    because that is the only one left, and every figure lands one column to the
    left. A page's headings are displaced by roughly the same amount, so
    offsets of (12.7, 105.8) are two different stories and (12.7, 17.7) is one
    story told twice.

    Columns left without a cluster take their heading plus the median
    displacement, which is an estimate, which is why `MONEY_ALIGN_TOLERANCE`
    has room in it.
    """
    if not found or not anchors:
        return None

    def admits(cluster: float, anchor: float, heading: tuple[float, float]) -> bool:
        # A heading is printed inside its own column, so a left-aligned column's
        # values start at or left of the heading's left edge and a right-aligned
        # column's figures end at or right of its right edge.
        if right_aligned:
            if cluster < anchor - INSIDE_SLACK:
                return False
        elif cluster > anchor + INSIDE_SLACK:
            return False
        # And the column's values have to sit UNDER that heading — not merely
        # somewhere to its left. Checking overlap globally was not enough: an
        # unmapped serial-number column overlaps nothing, was kept because it
        # overlapped *some other* anchor's heading, and was then handed to Date,
        # whose heading it is nowhere near. Every date cell came out as
        # `1 06-04-2024` and the import died on `unparseable date '1'`. §53.
        span = (under or {}).get(cluster)
        return span is None or _overlaps(span, heading)

    found = sorted(found)
    best: tuple[tuple[float, float], dict[str, float]] | None = None
    for slots in combinations(range(len(anchors)), min(len(found), len(anchors))):
        chosen = found[: len(slots)] if len(found) <= len(anchors) else None
        for pick in combinations(found, len(slots)) if chosen is None else [tuple(chosen)]:
            pairs = [
                (pick[i], anchors[slot][0], anchors[slot][1], anchors[slot][2])
                for i, slot in enumerate(slots)
            ]
            if not all(admits(c, a, h) for c, a, _f, h in pairs):
                continue
            offsets = [c - a for c, a, _f, _h in pairs]
            score = (_spread(offsets), sum(abs(o) for o in offsets))
            if best is None or score < best[0]:
                best = (score, {field: c for c, _a, field, _h in pairs})

    if best is None:
        return None
    out = dict(best[1])

    offsets = sorted(out[field] - anchor for anchor, field, _h in anchors if field in out)
    median = offsets[len(offsets) // 2]
    for anchor, field, _heading in anchors:
        out.setdefault(field, anchor + median)

    ordered = [out[field] for _anchor, field, _h in anchors]
    if ordered != sorted(ordered):
        log.info("pdf: learned edges are out of order (%s) — keeping the headings", ordered)
        return None
    return out


def _spread(offsets: list[float]) -> float:
    """How much the displacements disagree. Zero is a page that is simply
    shifted; large is an assignment that has paired the wrong things."""
    return (max(offsets) - min(offsets)) if offsets else 0.0


def _learn_columns(
    lines: list[list[dict]], columns: dict[str, tuple[float, float]], split: float
) -> tuple[dict[str, float], dict[str, float]]:
    """Where the columns really are: `(text left edges, money right edges)`.

    **A heading is not its column.** §48 learned the money edges after a bank
    was found whose figures did not line up with `Balance`; §51 is the same
    discovery on the other side, from a bank that **centres every heading over
    its column** where Canara aligns each one with its values. Measured, from a
    shape dump of a file nobody may read (§50):

        Date        heading 63.5-81.5     values 50.0-91.9
        Particulars heading 149.9-190.1   values from 105.0
        Balance     heading 512.7-542.3   figures ending 560.0

    Every rule anchored on a heading missed by 13 to 45 points. The narration
    landed in the date column, the figures landed nowhere, and the import failed
    with `row N: transaction has no balance` on a statement whose balance is
    printed plainly on the page.

    So both sides are learned. Text columns are left-aligned, so their **starts**
    cluster; money columns are right-aligned, so their **ends** do. Each column
    takes the nearest unclaimed cluster, and the result is used only if it still
    runs left to right — otherwise the headings stand, and the log says so.
    """
    text_anchors = sorted(
        (x0, field, columns[field])
        for field, (x0, _) in columns.items()
        if field not in MONEY_FIELDS
    )
    # Sorted by the heading's LEFT edge, so the order check compares like with
    # like — two money columns never overlap, so either edge orders them the
    # same way.
    money_anchors = [
        (columns[field][1], field, columns[field])
        for _x0, field in sorted(
            (columns[f][0], f) for f in columns if f in MONEY_FIELDS
        )
    ]

    text_fallback = {field: anchor for anchor, field, _h in text_anchors}
    money_fallback = {field: anchor for anchor, field, _h in money_anchors}
    if not lines:
        return text_fallback, money_fallback

    # Only clusters whose values sit UNDER the heading are candidates for it.
    # Without that filter an unmapped serial-number column at x 35 competes with
    # the real Date column at x 50 for a heading at 63.5, and wins on a tie the
    # geometry does not actually have (§51).
    spans = _spans([w for words in lines for w in words if w["x1"] <= split])
    under = {start: (start, end) for start, end in spans}
    starts = [
        start
        for start, end in spans
        if any(_overlaps((start, end), heading) for _a, _f, heading in text_anchors)
    ]
    ends = _cluster(
        [
            w["x1"]
            for words in lines
            for w in words
            if w["x1"] > split and _amount_text(w["text"]) is not None
        ]
    )

    text = _match(text_anchors, starts, right_aligned=False, under=under) or text_fallback
    money = _match(money_anchors, ends, right_aligned=True) or money_fallback
    log.info(
        "pdf columns: text %s money %s",
        {k: round(v, 1) for k, v in text.items()},
        {k: round(v, 1) for k, v in money.items()},
    )
    return text, money


def _split(columns: dict[str, tuple[float, float]]) -> float:
    """Where the money columns begin. Nothing left of this is a figure."""
    money = [x0 for field, (x0, _) in columns.items() if field in MONEY_FIELDS]
    return min(money) - MONEY_MARGIN if money else float("inf")


def _place(
    word: dict,
    columns: dict[str, tuple[float, float]],
    split: float,
    text: dict[str, float],
    money: dict[str, float],
) -> str:
    """Which field this word belongs to. See the module docstring for the rule.

    `text` and `money` are the **learned** edges (`_learn_columns`) — a column's
    real left or right edge, which is not its heading's.
    """
    if word["x1"] > split:
        # Among the money columns: nearest RIGHT edge, because they are
        # right-aligned and a wide figure starts further left than a narrow one.
        # And it has to be *near* one — see MONEY_ALIGN_TOLERANCE.
        if money:
            distance, field = min((abs(word["x1"] - x1), f) for f, x1 in money.items())
            if distance <= MONEY_ALIGN_TOLERANCE:
                return field
    # Otherwise: the last text column that starts at or before this word.
    starts = sorted(((x0, field) for field, x0 in text.items()), reverse=True)
    for x0, field in starts:
        if word["x0"] >= x0 - 2.0:
            return field
    if not starts:
        return "narration"
    # Left of every column the operator mapped. That is an **unmapped column** —
    # a serial number, a value date, a branch — and its contents are not wanted.
    # Folding them into the leftmost mapped column instead turned every date
    # cell into `1 01-08-2026`, which no date format reads.
    if word["x0"] < starts[-1][0] - UNMAPPED_MARGIN:
        return ""
    return starts[-1][1]


def _placer(
    columns: dict[str, tuple[float, float]],
    split: float,
    text: dict[str, float],
    money: dict[str, float],
):
    """`_place`, bound to one page's learned geometry."""

    def place(word: dict) -> str:
        return _place(word, columns, split, text, money)

    return place


def _placer_declared(at: dict[str, tuple[float, float]]):
    """Placement for a profile that measured its own columns. SPEC §99.

    A word belongs to the column whose declared range holds its **centre**, and
    nothing is inferred: no split, no learned edges, no alignment rule. That is
    the point — the alignment rule is what a declared layout exists to escape.
    SBI centres its figures, so the same column ends at 379.7 for one amount and
    383.1 for another, and `MONEY_ALIGN_TOLERANCE` would reject one of them.

    A word in no declared range belongs to a column nobody declared — SBI prints
    a Value Date beside the transaction date — and is dropped, exactly as the
    inferred path drops a word left of every mapped column.
    """
    ranges = sorted(at.items(), key=lambda pair: pair[1])

    def place(word: dict) -> str:
        centre = (word["x0"] + word["x1"]) / 2
        for field, (x0, x1) in ranges:
            if x0 <= centre <= x1:
                return field
        return ""

    return place


def _is_amount(text: str) -> bool:
    """`parse_amount` RAISES on a word that is not a figure, which is right for
    a spreadsheet cell and wrong for a question. Asked as a question here."""
    try:
        return parse_amount(text) is not None
    except ParseError:
        return False


def _row(words: list[dict], place) -> dict[str, str]:
    """One line's words as `{field: cell text}`. `place` says which field."""
    cells: dict[str, list[str]] = defaultdict(list)
    last_money: str | None = None

    for word in words:
        token = word["text"].strip()

        # `12,345.67 Cr` arrives as TWO words — pdfplumber splits on the space,
        # and a bare `Cr` is not a figure, so it would drift into the narration
        # of every single row. Attached to the amount it follows, where it
        # belongs: `Cr` is the ordinary case and `Dr` means overdrawn. §51.
        if last_money and token.lower() in _SUFFIX and cells[last_money]:
            if _SUFFIX[token.lower()] < 0:
                figure = cells[last_money][-1]
                cells[last_money][-1] = figure[1:] if figure.startswith("-") else "-" + figure
            continue

        field = place(word)
        if not field:
            last_money = None
            continue
        if field in MONEY_FIELDS:
            if token in MONEY_PLACEHOLDERS:
                # An empty money cell, written down. Not a figure and not a
                # label — dropping it is what makes the cell read as empty.
                last_money = None
                continue
            # Normalised here, so the cell handed downstream is a plain figure:
            # `12,345.67 Cr` is not something `parse_amount` should have to know
            # about, and `Dr` has already been turned into a negative.
            figure = _amount_text(token)
            if figure is not None:
                cells[field].append(figure)
                last_money = field
                continue
            # A word in a money column that is not money is not money. This is
            # what stops `Opening Balance` — a label printed under the
            # Withdrawals column on some banks' paper — reaching parse_amount,
            # and it is why the sentinel rows survive banding.
            field = "narration"
        last_money = None
        cells[field].append(word["text"])
    return {field: " ".join(parts) for field, parts in cells.items()}


def _wraps_declared(place) -> "callable":
    """Whether a line is a wrapped narration, for a declared layout. §99.

    **Every word of it is in the description column**, and words in a column
    nobody declared do not count against it. That is a stronger test than the
    inferred path's "it starts under the narration column", and it has to be:
    a declared range is deliberately wider than the text it holds, so SBI's
    page footer starts inside the description column and is not a description.
    It carries a page number in the reference column, which no wrapped line
    ever does — so looking at the whole line settles it and looking at the
    first word cannot.
    """

    def wraps(words: list[dict]) -> bool:
        fields = {place(word) for word in words}
        return "narration" in fields and not (fields - {"narration", ""})

    return wraps


def _declared_layout(
    lines: list[list[dict]], wanted: dict[str, str], at: dict[str, tuple[float, float]]
):
    """Where the table starts, for a profile that measured its columns. §99.

    Returns `(first table line, {field: heading}, placer)`, or None if no line
    on the page reads as a transaction — which is the same "this is not that
    bank" answer `find_header` gives.

    **There is no header line to find.** SBI's headings are a background
    graphic; only the word `Balance` is text, and `find_header` wants four
    names before it will believe a line. So the table is located by its
    *content* instead: the first line that bands into a date and a balance is
    the first transaction, and everything above it is preamble.

    That is a stricter test than it looks. Nothing in this file's 71 preamble
    lines carries a figure in the balance range — the account number and the
    customer id both sit 40pt short of it — so the boundary is not a near miss
    that happened to fall the right way.

    The one thing it gets wrong on its own is the **first** transaction: a
    description block starts a few points above its own dated line, so line 72
    of 529 belongs to the row on line 73 and would otherwise be filed as
    preamble, silently costing the first payee its opening words. So the
    boundary walks back over lines that are purely narration and sit within one
    block's overhang of that first row.
    """
    place = _placer_declared(at)
    first = None
    for index, words in enumerate(lines):
        cells = _row(words, place)
        if cells.get("date", "").strip() and _is_amount(cells.get("balance", "")):
            first = index
            break
    if first is None:
        return None

    top = lines[first][0]["top"]
    while first > 0:
        above = lines[first - 1]
        if top - above[0]["top"] > BLOCK_OVERHANG:
            break
        if not _wraps_declared(place)(above):
            break
        first -= 1

    # The profile's own words for its columns, keyed by field. `from_rows`
    # re-runs `_find_header` over the grid this produces and matches header
    # TEXT, so the synthesised row has to carry names the profile registered as
    # aliases — which is exactly what `wanted` is, already normalised.
    headings = {field: heading for heading, field in wanted.items()}
    log.info("pdf table declared at line %d: %s", first, sorted(at))
    return first, headings, place


def to_grid(
    lines: list[list[dict]],
    wanted: dict[str, str],
    order: list[str],
    *,
    at: dict[str, tuple[float, float]] | None = None,
) -> list[list[str]] | None:
    """A PDF's lines as a rectangular grid `from_rows` can read, or None.

    `order` fixes the column order of the output — the same order the header row
    is written in — so downstream sees exactly what it sees for a spreadsheet.

    `at` is a profile's `columns_at` (§99): the x-range of each column, measured
    rather than inferred, for a bank that does not print its headings as text.
    Without it the header is found by its column names and the edges are learned
    from the data, which is what every bank that prints a header gets.

    Above the header, lines are split on wide gaps into cells rather than banded:
    a preamble does not follow the table's columns, and `_find_metadata` reads
    label/value pairs out of the first cells of a row.

    A line with no date cell is a **continuation** — the bank wrapping one
    transaction's narration onto the next line — and its narration is appended to
    the row it belongs to rather than becoming a row of its own.
    """
    if at:
        located = _declared_layout(lines, wanted, at)
        if located is None:
            return None
        header_at, headings, place = located
        # No header line to skip and none to repeat: the declared path never
        # matches one, and a page's repeated headings are page furniture like
        # any other — they carry no date and no figure.
        data_from = header_at
        def repeated(_words: list[dict]) -> bool:
            return False

        wraps = _wraps_declared(place)
    else:
        header = find_header(lines, wanted)
        if header is None:
            return None
        header_at, columns, headings = header
        split = _split(columns)
        # **Not the repeated headers.** The header prints again at the top of
        # every page, and its words sit at the HEADING positions — so on a
        # six-page statement they form a cluster of six at exactly the heading's
        # own x, whose offset from that heading is zero. `_match` scores by how
        # consistent the offsets are, and nothing is more consistent than zero:
        # it learned the headings as the columns, which is the fallback wearing
        # a disguise.
        #
        # Measured. The narration then started at 149.9 instead of 105.0, so a
        # wrapped line at x 105 landed in the DATE column, which made it look
        # like a row rather than a continuation, and that row had no balance.
        # One page of test data never showed it; six pages of a real statement
        # did. §53.
        learn_from = [
            words for words in lines[header_at + 1 :] if find_header([words], wanted) is None
        ]
        text_edges, money_edges = _learn_columns(learn_from, columns, split)
        place = _placer(columns, split, text_edges, money_edges)
        data_from = header_at + 1

        def repeated(words: list[dict]) -> bool:
            # Detected exactly — it matches the profile's own column names —
            # rather than by looking for a line that resembles a header.
            return find_header([words], wanted) is not None

        # The LEARNED start, not the heading's: a continuation wraps under the
        # column's real left edge, which on a centred-heading bank is nowhere
        # near the word `Particulars`.
        narration_x0 = text_edges.get("narration", columns.get("narration", (0.0, 0.0))[0])

        def wraps(words: list[dict]) -> bool:
            return bool(words) and words[0]["x0"] <= narration_x0 + 4.0

    grid: list[list[str]] = []
    for words in lines[:header_at]:
        grid.append([_text(cell) for cell in _group(words, PREAMBLE_GAP)])
    # The bank's own words, in `order`'s positions. `_find_header` runs over
    # this grid exactly as it does over a spreadsheet, and it matches header
    # TEXT — a row of passbook's field names would match nothing.
    grid.append([headings.get(field, field) for field in order])

    # Wrapped lines and the vertical position of every dated row, so each
    # continuation can be attached to the row it belongs to rather than to
    # whichever row happened to be parsed last. §96. Each pending line records
    # **how many rows preceded it**, so "the row before" and "the row after"
    # are reading order and not a coordinate comparison — `top` restarts at the
    # top of every page, and a document is a stack of pages.
    pending: list[tuple[float, str, int]] = []
    # `(top, index into grid)`. The INDEX is recorded rather than computed,
    # because `grid` also receives sentinel rows and those carry no top — a
    # position derived from the count of dated rows drifts by one per sentinel,
    # which is exactly far enough to hand every description to its neighbour.
    tops: list[tuple[float, int]] = []

    for words in lines[data_from:]:
        if repeated(words):
            continue

        cells = _row(words, place)
        if not cells:
            continue

        if not cells.get("date", "").strip():
            # No date. Three things look like this and they are told apart by
            # geometry, not by reading the words:
            #
            # * an **Opening/Closing Balance sentinel** — no date, but a figure
            #   squarely in the balance column. Kept as a row, so `_find_sentinel`
            #   can still use it: a stated closing balance is an independent
            #   cross-check that a derived one is not (§42).
            # * a **wrapped narration** — continues underneath the column it
            #   wrapped in, so it starts at the narration column's left edge.
            #   Measured: continuations begin at exactly x 85.0, the Particulars
            #   heading's own x0.
            # * **page furniture** — `Page 1 of 6` at x 503-539, under nothing.
            #   Skipped. Before this, its `1` banded into Deposits as a credit of
            #   one rupee and `Page of` was glued onto a real transaction.
            if _is_amount(cells.get("balance", "")):
                grid.append([cells.get(field, "") for field in order])
                continue
            if not wraps(words):
                continue
            extra = cells.get("narration", "").strip()
            if extra:
                # **Deferred, not appended to the row above.** §96.
                #
                # A description block does not begin on its own dated line. On
                # one bank it is centred on the row, so a three-line narration
                # prints one line ABOVE the dated line and one below it:
                #
                #     x0=105   ...Int.Pd:01-01-      <- belongs to the row below
                #     x0=35    06-04-2024  ...       <- the transaction
                #     x0=105   2024 to 31-03-2024    <- belongs to the row above
                #
                # Appending every continuation to `grid[-1]` put the first half
                # on the PREVIOUS transaction. Measured on a real 32-row
                # statement: every payee came out `(unparsed)`, and seven rows
                # tripped the direction check because a `CR` description had
                # landed on a withdrawal.
                #
                # Which row it belongs to is settled below, once every row's
                # position is known.
                pending.append((words[0]["top"], extra, len(tops)))
            continue

        grid.append([cells.get(field, "") for field in order])
        tops.append((words[0]["top"], len(grid) - 1))

    _reattach(grid, order, pending, tops)

    log.info(
        "pdf banded into %d rows x %d columns, %d wrapped line(s) reattached",
        len(grid),
        len(order),
        len(pending),
    )
    return grid


def _reattach(
    grid: list[list[str]],
    order: list[str],
    pending: list[tuple[float, str, int]],
    tops: list[tuple[float, int]],
) -> None:
    """Put each wrapped line on the transaction it belongs to. SPEC §96, §99.

    **The row before it, unless the row after it starts underneath it.** A
    description block begins a few points above its own dated line and runs
    down, so a continuation printed within `BLOCK_OVERHANG` above the next
    transaction is that transaction's opening line; anything else continues the
    one before.

    Nearest-first was the first rule and it is wrong on a bank whose block is
    not centred on its row. Measured on two:

        Union   block spans -5.5 to +5.5, rows 20pt apart
        SBI     block spans -7.1 to +30.1, rows 50pt apart

    SBI's last description line is 30pt below its own row and 20pt above the
    next, so nearest-first hands the tail of every description to the following
    transaction.

    **Reading order, not coordinates.** `top` is measured from the top of a
    page and restarts on every one, so "the row above" cannot be found by
    comparing tops across a 7-page statement — a continuation at y 700 on page
    one would happily adopt a row at y 100 on page two. The look-ahead is
    guarded by the same reasoning: a row that belongs to a continuation is
    below it on the same page, never 600 points above it.

    Continuations above the FIRST transaction have no row to belong to, and are
    dropped rather than guessed at.
    """
    if not pending or not tops:
        return
    position = order.index("narration")
    for top, extra, rows_before in pending:
        index: int | None = None
        if rows_before < len(tops):
            next_top, next_index = tops[rows_before]
            if top - 1.0 <= next_top <= top + BLOCK_OVERHANG:
                index = next_index
        if index is None and rows_before:
            index = tops[rows_before - 1][1]
        if index is None:
            continue
        grid[index][position] = (grid[index][position] + " " + extra).strip()


# --- describing a page without reading it. SPEC §50 --------------------------


def shape(text: str) -> str:
    """A token's *shape*, with every character thrown away.

    `09-05-2026` -> `99-9999-9999`? No: runs are collapsed, so it is `99-99-9999`
    and `1,418.91` is `9,999.99`. `ZEPKV` is `AAAAA` -> `A5`. The point is that
    two tokens with the same shape are interchangeable to a parser and that the
    shape carries no information about the person.

    This exists because the operator's file cannot be shared and the geometry is
    what a banding bug is made of. A shape plus an x-range is enough to debug
    every failure this module has had, and it contains no name, no payee, no
    account number and no amount.
    """
    out: list[str] = []
    run_kind, run_len = "", 0

    def flush() -> None:
        if not run_kind:
            return
        out.append(run_kind if run_len == 1 else f"{run_kind}{run_len}")

    for char in text:
        kind = "9" if char.isdigit() else "A" if char.isalpha() else char
        if kind == run_kind and kind in {"9", "A"}:
            run_len += 1
            continue
        flush()
        run_kind, run_len = kind, 1
    flush()
    return "".join(out)


def describe(lines: list[list[dict]], limit: int = 40) -> list[dict]:
    """A page as geometry and shapes: shareable, and useless to a thief.

    One entry per line, each word reduced to `(shape, x0, x1)`. Nothing here can
    be turned back into a statement — `9,999.99` was an amount and there is no
    way to learn which.
    """
    out = []
    for index, words in enumerate(lines[:limit]):
        out.append(
            {
                "line": index,
                "words": [
                    {
                        "shape": shape(word["text"]),
                        "x0": round(word["x0"], 1),
                        "x1": round(word["x1"], 1),
                    }
                    for word in words
                ],
            }
        )
    return out
