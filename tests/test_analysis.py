"""The exclusion semantics, pinned. SPEC §18, §8, §8.1.

`service.ledger_analysis` is the one place that decides what counts as spend and
what counts as earnings. Getting it wrong is not a rounding error. Measured on
one real three-month ledger, the naive by-type reading was **three times** the
true spend and **1.6 times** the true earnings — and a chart drawn on the naive
numbers looks entirely reasonable. So every branch of the rule gets a test.

No network: the function takes Firefly's split dicts as data, which is the whole
reason it takes them as data.
"""

from __future__ import annotations

from datetime import date, time
from decimal import Decimal

import pytest

from passbook import service

# A rules.yaml, reduced to what the analysis reads. `food` is a tag on category
# rules, exactly as the real file carries it — the roll-up is not separately
# configured anywhere.
RULES = {
    "rules": [
        {"category": "Morning Stall", "tag": "food"},
        {"category": "Eating Out", "tag": "food"},
        {"category": "Shopping"},
        {"category": "Investments"},
        {"category": "Salary"},
    ],
    "not_spend": ["Investments", "Transfers"],
}


def split(
    kind: str,
    amount: str,
    *,
    category: str | None = None,
    tags: tuple[str, ...] = (),
    when: str = "2026-06-10",
    external_id: str | None = None,
) -> dict:
    """One Firefly transaction split, in the shape the API actually returns.

    Field names read off the live v6.6.6 response, not from memory: `type`,
    `amount` as an over-precise string, `category_name`, `tags`, `external_id`,
    and a `date` carrying a timezone offset.
    """
    return {
        "type": kind,
        "amount": f"{Decimal(amount):.12f}",
        "category_name": category,
        "tags": list(tags),
        "external_id": external_id,
        "date": f"{when}T00:00:00+05:30",
    }


def test_movement_is_not_spending():
    """The whole point. A fund purchase leaves the account and is not spend."""
    result = service.ledger_analysis(
        [
            split("withdrawal", "100.00", category="Shopping"),
            split("withdrawal", "30000.00", category="Investments"),
            split("withdrawal", "150.00", category="Transfers"),
        ],
        rules=RULES,
    )
    assert result.gross_spend == Decimal("30250.00")
    assert result.spend == Decimal("100.00")
    assert [(s.name, s.amount) for s in result.excluded_spend] == [
        ("Investments", Decimal("30000.00")),
        ("Transfers", Decimal("150.00")),
    ]
    # And the two halves still add up to what the bank actually took out.
    assert result.spend + sum(s.amount for s in result.excluded_spend) == result.gross_spend


def test_money_coming_back_is_not_earnings():
    """§8.1's inversion: earnings are Salary and Interest Income, nothing else."""
    result = service.ledger_analysis(
        [
            split("deposit", "20000.00", category="Salary"),
            split("deposit", "5000.00", tags=("not-earnings",)),
            split("deposit", "1.00", category="Verification", tags=("not-earnings",)),
        ],
        rules=RULES,
    )
    assert result.gross_income == Decimal("25001.00")
    assert result.income == Decimal("20000.00")
    assert result.excluded_income.amount == Decimal("5001.00")
    assert result.excluded_income.count == 2


def test_a_tag_can_never_remove_a_withdrawal_from_spend():
    """§8.1 guarantees `not-earnings` only ever lands on a deposit, because the
    rule triggers on `transaction_type = deposit`. If that guarantee were ever
    broken, real spending must NOT silently vanish from the spend figure — which
    is the exact footgun §8.1 records an earlier payee-list version having."""
    result = service.ledger_analysis(
        [split("withdrawal", "500.00", category="Shopping", tags=("not-earnings",))],
        rules=RULES,
    )
    assert result.spend == Decimal("500.00")
    assert result.excluded_income.amount == Decimal(0)


def test_an_opening_balance_is_neither_spend_nor_income():
    """Firefly's own type for it. It is on the account and is not a transaction
    the bank made — `purge` excludes it structurally for the same reason (§7.3)."""
    result = service.ledger_analysis(
        [
            split("opening balance", "10000.00"),
            split("withdrawal", "65.00", category="Shopping"),
        ],
        rules=RULES,
    )
    assert result.gross_spend == Decimal("65.00")
    assert result.gross_income == Decimal(0)
    assert result.withdrawals == 1
    assert result.deposits == 0


def test_categories_come_back_largest_first_and_unruled_rows_are_named():
    result = service.ledger_analysis(
        [
            split("withdrawal", "10.00", category="Shopping"),
            split("withdrawal", "99.00", category="Eating Out"),
            split("withdrawal", "50.00", category=None),
        ],
        rules=RULES,
    )
    assert [s.name for s in result.categories] == ["Eating Out", "(no category)", "Shopping"]
    # An unruled row is still spend — it is money that left — and it is named
    # rather than dropped, because it is also work to do on Payees.
    assert result.uncategorised.amount == Decimal("50.00")
    assert result.uncategorised.count == 1
    assert result.spend == Decimal("159.00")


def test_a_rollup_totals_the_tag_and_lists_the_categories_that_carry_it():
    """Two sources for one number, on purpose: the total is the tag as Firefly
    stored it, the segments are the categories tagged in rules.yaml. If they ever
    disagree the stacked bar will not fill, which is visible."""
    result = service.ledger_analysis(
        [
            split("withdrawal", "300.00", category="Eating Out", tags=("food",)),
            split("withdrawal", "45.00", category="Morning Stall", tags=("food",)),
            split("withdrawal", "999.00", category="Shopping"),
        ],
        rules=RULES,
    )
    assert len(result.rollups) == 1
    rollup = result.rollups[0]
    assert rollup.tag == "food"
    assert rollup.amount == Decimal("345.00")
    assert rollup.count == 2
    assert [(p.name, p.amount) for p in rollup.parts] == [
        ("Eating Out", Decimal("300.00")),
        ("Morning Stall", Decimal("45.00")),
    ]
    assert sum(p.amount for p in rollup.parts) == rollup.amount


def test_an_excluded_category_stays_out_of_its_rollup_too():
    """Otherwise `food` could be inflated by a row the spend figure excludes, and
    the tag total would not match the sum of its own segments."""
    result = service.ledger_analysis(
        [
            split("withdrawal", "300.00", category="Eating Out", tags=("food",)),
            split("withdrawal", "500.00", category="Investments", tags=("food",)),
        ],
        rules=RULES,
    )
    assert result.rollups[0].amount == Decimal("300.00")


def test_the_clock_comes_from_the_statement_not_from_the_ledger():
    """`txn_time` is parsed out of the narration (§6.5) and never pushed, so
    Firefly has no idea what time of day anything happened. The join is on
    `external_id`, which is the bank's own transaction id (§6.1)."""
    result = service.ledger_analysis(
        [
            split("withdrawal", "65.00", category="Shopping", external_id="20260610000001"),
            split("withdrawal", "20.00", category="Shopping", external_id="20260610000002"),
            split("withdrawal", "10.00", category="Investments", external_id="20260610000003"),
        ],
        times={
            "20260610000001": time(1, 51, 33),
            "20260610000002": None,          # a NEFT/CHG/INT row: no clock at all
            "20260610000003": time(9, 0, 0),  # excluded from spend, so not plotted
        },
        rules=RULES,
    )
    assert result.hours[1] == 1
    assert result.hours[9] == 0, "an excluded row must not appear in the day chart"
    assert sum(result.hours) == result.clocked == 1
    # The two denominators are different and both are reported. Labelling a
    # chart "N transactions" with N = clocked is the §16.10 defect.
    assert result.counted == 2


def test_partial_months_are_marked_from_the_statement_coverage():
    """A weekly export runs mid-month to mid-month, so the first and last buckets
    of any range are stubs. This is what stops a trend line being drawn."""
    result = service.ledger_analysis(
        [
            split("withdrawal", "10.00", category="Shopping", when="2026-05-20"),
            split("withdrawal", "20.00", category="Shopping", when="2026-06-15"),
            split("withdrawal", "30.00", category="Shopping", when="2026-07-31"),
        ],
        coverage=(date(2026, 5, 7), date(2026, 7, 31)),
        rules=RULES,
    )
    assert [(m.month, m.partial) for m in result.months] == [
        ("2026-05", True),   # coverage starts on the 7th
        ("2026-06", False),  # whole month covered
        ("2026-07", False),  # coverage ends exactly on the 31st
    ]
    assert [m.spend for m in result.months] == [
        Decimal("10.00"),
        Decimal("20.00"),
        Decimal("30.00"),
    ]


def test_a_month_ending_one_day_short_is_still_partial():
    result = service.ledger_analysis(
        [split("withdrawal", "10.00", category="Shopping", when="2026-07-15")],
        coverage=(date(2026, 7, 1), date(2026, 7, 30)),
        rules=RULES,
    )
    assert result.months[0].partial is True


def test_every_amount_is_a_decimal_and_the_over_precision_is_quantised():
    """Firefly sends `'48.000000000000'`. CLAUDE.md non-negotiable #1 does not
    stop at the process boundary, so nothing here ever becomes a float."""
    result = service.ledger_analysis(
        [split("withdrawal", "48.00", category="Shopping")], rules=RULES
    )
    assert isinstance(result.spend, Decimal)
    assert str(result.spend) == "48.00"
    assert all(isinstance(s.amount, Decimal) for s in result.categories)


def test_a_refund_is_counted_and_reported_separately():
    """A reversal posts as an ordinary deposit and is tagged by the pusher
    (§7.2). It is excluded from earnings and reported on its own, because it is
    a spend coming back — netting it into a month it did not happen in is the
    more misleading of the two options."""
    result = service.ledger_analysis(
        [split("deposit", "48.00", tags=("reversal", "not-earnings"))], rules=RULES
    )
    assert result.refunds.amount == Decimal("48.00")
    assert result.refunds.count == 1
    assert result.income == Decimal(0)


def test_an_empty_ledger_produces_zeroes_not_a_crash():
    """The state a fresh install is in, and the state the DR drill lands in
    between the restore and the first push."""
    result = service.ledger_analysis([], rules=RULES)
    assert result.spend == result.income == Decimal(0)
    assert result.categories == []
    assert result.months == []
    assert result.hours == [0] * 24


def test_not_spend_and_the_rollups_are_read_from_config_not_hardcoded():
    """These are the operator's own category names (D10), so they live in
    rules.yaml. A category named there that does not exist excludes nothing."""
    assert service.load_not_spend({"not_spend": ["Nope"]}) == ["Nope"]
    assert service.load_not_spend({}) == []
    assert service.tag_rollups(RULES) == {
        "food": ["Morning Stall", "Eating Out"],
    }
    assert service.tag_rollups({"rules": [{"category": "X"}]}) == {}


# --- against the fixture, so the arithmetic is checked on real amounts -------


@pytest.fixture
def fixture_splits(parsed):
    """The 93 fixture rows as Firefly splits.

    Amounts, dates and ids come from `tests/fixtures/statement.xls` through the
    parser — §16.6: every row shown or asserted anywhere comes from the fixture,
    not from a hand-typed table. Categories and tags are assigned here because
    the fixture's payees are redacted nonsense that no rule matches, and the
    point of this test is the arithmetic over real amounts.
    """
    meta, transactions = parsed
    splits = []
    for index, txn in enumerate(transactions):
        if txn.debit is not None:
            category = ("Investments", "Transfers", "Shopping", "Eating Out")[index % 4]
            tags = ("food",) if category == "Eating Out" else ()
            splits.append(
                split(
                    "withdrawal",
                    str(txn.debit),
                    category=category,
                    tags=tags,
                    when=txn.txn_date.isoformat(),
                    external_id=txn.txn_id,
                )
            )
        else:
            tagged = ("not-earnings",) if index % 3 else ()
            splits.append(
                split(
                    "deposit",
                    str(txn.credit),
                    category="Salary" if not tagged else None,
                    tags=tagged,
                    when=txn.txn_date.isoformat(),
                    external_id=txn.txn_id,
                )
            )
    return meta, transactions, splits


def test_the_gross_figures_equal_the_statement_totals(fixture_splits):
    """The cross-check that matters: whatever the exclusions do, the gross
    figures have to be the statement's own withdrawal and deposit totals."""
    from passbook.service import ParsedStatement

    meta, transactions, splits = fixture_splits
    statement = ParsedStatement(path=None, meta=meta, transactions=transactions)  # type: ignore[arg-type]
    result = service.ledger_analysis(splits, rules=RULES)

    assert result.gross_spend == statement.debits
    assert result.gross_income == statement.credits
    assert result.withdrawals + result.deposits == len(transactions) == 93


def test_the_excluded_and_counted_parts_always_reconstruct_the_gross(fixture_splits):
    _, _, splits = fixture_splits
    result = service.ledger_analysis(splits, rules=RULES)

    assert result.spend + sum(s.amount for s in result.excluded_spend) == result.gross_spend
    assert result.income + result.excluded_income.amount == result.gross_income
    # And the exclusions are actually doing something on this data, so the
    # assertions above are not vacuously true.
    assert result.spend < result.gross_spend
    assert result.income < result.gross_income


def test_the_day_chart_only_plots_rows_that_count_as_spend(fixture_splits):
    _, transactions, splits = fixture_splits
    times = {t.txn_id: t.txn_time for t in transactions}
    result = service.ledger_analysis(splits, times=times, rules=RULES)

    assert sum(result.hours) == result.clocked <= result.counted
    assert result.counted < result.withdrawals, "some withdrawals are excluded movement"
