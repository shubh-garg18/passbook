"""The exclusion semantics, pinned. SPEC §18, §8, §8.1.

`service.ledger_analysis` is the one place that decides what counts as spend and
what counts as earnings. Getting it wrong is not a rounding error: measured on
one real three-month ledger, the naive by-type reading was **three times** the
true spend and **1.6 times** the true earnings, and a chart of the naive numbers
looks entirely reasonable. So every branch of the rule gets a test.

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
        {"category": "Day Canteen", "tag": "food"},
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
            split("opening balance", "12612.64"),
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
            split("withdrawal", "45.00", category="Day Canteen", tags=("food",)),
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
        ("Day Canteen", Decimal("45.00")),
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
    """Firefly sends `'48.000000000000'`. Non-negotiable #1 does not
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
        "food": ["Day Canteen", "Eating Out"],
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


# --- §57: the balance path, the payee split, and category by month ----------


def txn(txn_id: str, day: str, balance: str, *, debit=None, credit=None):
    """One statement row. The balance is the BANK's running figure (§6.6)."""
    from passbook.models import Transaction

    return Transaction(
        txn_id=txn_id,
        txn_date=date.fromisoformat(day),
        narration="x",
        debit=Decimal(debit) if debit else None,
        credit=Decimal(credit) if credit else None,
        balance=Decimal(balance),
    )


def test_the_balance_path_is_the_banks_own_figure_never_accumulated():
    """The whole point of §57's line: it reads balances, it does not add up.

    A chart that accumulated debits and credits would be a second implementation
    of the continuity invariant and would agree with it right up until it did
    not — so the test feeds a chain whose movements do NOT reconstruct the
    balances, and asserts the recorded figures win.
    """
    rows = [
        txn("20260509000001", "2026-05-09", "1000.00", credit="1000.00"),
        # A movement that does not explain the next balance. The bank's figure
        # is still the answer.
        txn("20260510000001", "2026-05-10", "7777.77", debit="1.00"),
    ]
    points, opening = service.balance_series(rows)
    assert opening is None
    assert [(p.day, p.balance) for p in points] == [
        ("2026-05-09", Decimal("1000.00")),
        ("2026-05-10", Decimal("7777.77")),
    ]


def test_a_day_collapses_to_its_closing_figure():
    rows = [
        txn("20260509000001", "2026-05-09", "900.00", debit="100.00"),
        txn("20260509000002", "2026-05-09", "800.00", debit="100.00"),
        txn("20260509000003", "2026-05-09", "750.00", debit="50.00"),
    ]
    points, _ = service.balance_series(rows)
    assert len(points) == 1
    assert points[0].balance == Decimal("750.00")


def test_rows_out_of_order_still_close_the_day_correctly():
    """`account_transactions` merges overlapping statements, so arrival order is
    not sheet order. The id carries `YYYYMMDD` + a daily sequence (§6.1)."""
    rows = [
        txn("20260509000003", "2026-05-09", "750.00"),
        txn("20260509000001", "2026-05-09", "900.00"),
        txn("20260509000002", "2026-05-09", "800.00"),
    ]
    points, _ = service.balance_series(rows)
    assert points[0].balance == Decimal("750.00")


def test_a_window_keeps_the_last_balance_before_it_as_the_opening():
    """Without this the first in-window transaction reads as the opening
    balance, which it is not."""
    rows = [
        txn("20260501000001", "2026-05-01", "100.00"),
        txn("20260520000001", "2026-05-20", "200.00"),
        txn("20260610000001", "2026-06-10", "300.00"),
        txn("20260720000001", "2026-07-20", "400.00"),
    ]
    points, opening = service.balance_series(
        rows, start=date(2026, 6, 1), end=date(2026, 6, 30)
    )
    assert opening is not None
    # The LAST one before the window, not the first.
    assert (opening.day, opening.balance) == ("2026-05-20", Decimal("200.00"))
    assert [p.day for p in points] == ["2026-06-10"]


def test_an_empty_account_has_no_line_and_does_not_crash():
    assert service.balance_series([]) == ([], None)


def test_category_months_align_with_months_by_position_and_sum_to_the_category():
    result = service.ledger_analysis(
        [
            split("withdrawal", "100.00", category="Shopping", when="2026-05-04"),
            split("withdrawal", "40.00", category="Shopping", when="2026-07-09"),
            split("withdrawal", "7.00", category="Eating Out", when="2026-06-02"),
        ],
        rules=RULES,
    )
    months = [m.month for m in result.months]
    assert months == ["2026-05", "2026-06", "2026-07"]

    by_name = {c.name: c for c in result.category_months}
    # Zero-padded, so a month a category never appears in is a gap rather than
    # a missing point that shifts every later one left.
    assert by_name["Shopping"].amounts == [Decimal("100.00"), Decimal(0), Decimal("40.00")]
    assert by_name["Eating Out"].amounts == [Decimal(0), Decimal("7.00"), Decimal(0)]

    # Same order as `categories`, and the carried total matches — the client
    # must never add these up itself (§16.1 forbids money through a float).
    assert [c.name for c in result.category_months] == [s.name for s in result.categories]
    for series, slice_ in zip(result.category_months, result.categories):
        assert series.total == slice_.amount == sum(series.amounts)


def test_an_excluded_category_never_reaches_the_month_grid():
    """`not_spend` is movement, not spending (non-negotiable 9). It is absent
    from `categories`, so it must be absent here or the two disagree."""
    result = service.ledger_analysis(
        [
            split("withdrawal", "500.00", category="Investments", when="2026-05-04"),
            split("withdrawal", "10.00", category="Shopping", when="2026-05-04"),
        ],
        rules=RULES,
    )
    assert [c.name for c in result.category_months] == ["Shopping"]


def test_payees_and_sources_carry_the_exclusions_like_every_other_figure():
    """Firefly's own expense/revenue report counts everything. This must not."""
    rows = [
        {**split("withdrawal", "500.00", category="Investments"), "destination_name": "Broker"},
        {**split("withdrawal", "30.00", category="Shopping"), "destination_name": "Shop"},
        {**split("withdrawal", "20.00", category="Shopping"), "destination_name": "Shop"},
        {**split("deposit", "900.00", category="Salary"), "source_name": "Employer"},
        {
            **split("deposit", "50.00", tags=("not-earnings",)),
            "source_name": "Self",
        },
    ]
    result = service.ledger_analysis(rows, rules=RULES)

    assert [(p.name, p.amount, p.count) for p in result.payees] == [
        ("Shop", Decimal("50.00"), 2)
    ]
    # §72. Every deposit, including the not-earnings one — "who paid you" and
    # "what did you earn" are two questions and only the second excludes. The
    # operator found this: money from family was missing from a report headed
    # "inside each source, what the money was booked as".
    assert [(s.name, s.amount) for s in result.sources] == [
        ("Employer", Decimal("900.00")),
        ("Self", Decimal("50.00")),
    ]
    # Each side reconciles against the figure it belongs to, and they are
    # deliberately different figures.
    assert sum(p.amount for p in result.payees) == result.spend
    assert sum(s.amount for s in result.sources) == result.gross_income
    assert result.gross_income != result.income


def test_net_is_the_change_in_the_balance_not_earned_less_spent():
    """§62. The KPI card computed `income - spend` and captioned it "earned less
    spent". Both of those already have movement removed, so their difference
    counts nothing that moved — it read more than three times the balance's
    actual movement over one window.
    """
    result = service.ledger_analysis(
        [
            split("withdrawal", "100.00", category="Shopping"),
            # Movement: excluded from `spend`, but it really left the account.
            split("withdrawal", "500.00", category="Investments"),
            split("deposit", "900.00", category="Salary"),
        ],
        rules=RULES,
    )
    assert result.spend == Decimal("100.00")
    assert result.income == Decimal("900.00")
    # The tempting, wrong figure.
    assert result.income - result.spend == Decimal("800.00")
    # What the balance actually did.
    assert result.net == Decimal("300.00")
    assert result.net == result.gross_income - result.gross_spend


def test_net_does_not_depend_on_which_categories_are_called_movement():
    """`not_spend` is the operator's list and it changes (§62). `net` must be
    the same figure either way, because the money moved either way."""
    rows = [
        split("withdrawal", "100.00", category="Shopping"),
        split("withdrawal", "500.00", category="Investments"),
        split("deposit", "900.00", category="Salary"),
    ]
    excluded = service.ledger_analysis(rows, rules={**RULES, "not_spend": ["Investments"]})
    counted = service.ledger_analysis(rows, rules={**RULES, "not_spend": []})
    assert excluded.spend != counted.spend
    assert excluded.net == counted.net == Decimal("300.00")


# --- §73: a bill that settles another month's spending -----------------------


def test_a_card_bill_paid_early_is_bucketed_to_the_previous_month():
    """> "I pay Credit Card bill in first 10days of the month but it is of
    >  previous month"

    Bucketed on its own date the bill puts last month's purchases in this
    month, and every month chart is then wrong by a bill.
    """
    rows = [split("withdrawal", "9000.00", category="Credit Card", when="2026-08-05")]
    plain = service.ledger_analysis(rows, rules=CARD_RULES)
    assert {m.month: m.spend for m in plain.months} == {"2026-08": Decimal("9000.00")}

    shifted = service.ledger_analysis(
        rows,
        rules=CARD_RULES,
        attribution=service.Attribution(categories=frozenset({"Credit Card"})),
    )
    assert {m.month: m.spend for m in shifted.months} == {"2026-07": Decimal("9000.00")}


def test_part_of_a_bill_can_stay_in_the_month_it_was_paid():
    """The operator's exception: "in this 5000 should be of Aug only"."""
    rows = [
        split(
            "withdrawal", "14160.69", category="Credit Card",
            when="2026-08-05", external_id="canara-1111-20260805000001",
        )
    ]
    result = service.ledger_analysis(
        rows,
        rules=CARD_RULES,
        attribution=service.Attribution(
            categories=frozenset({"Credit Card"}),
            keep={"canara-1111-20260805000001": Decimal("5000.00")},
        ),
    )
    assert {m.month: m.spend for m in result.months} == {
        "2026-07": Decimal("9160.69"),
        "2026-08": Decimal("5000.00"),
    }


def test_attribution_moves_no_money_only_which_month_it_lands_in():
    """The invariant that makes this safe. A reporting shift that changed a
    total would be inventing or destroying money."""
    rows = [
        split("withdrawal", "9000.00", category="Credit Card", when="2026-08-05"),
        split("withdrawal", "100.00", category="Shopping", when="2026-08-06"),
    ]
    a = service.Attribution(categories=frozenset({"Credit Card"}))
    plain = service.ledger_analysis(rows, rules=CARD_RULES)
    shifted = service.ledger_analysis(rows, rules=CARD_RULES, attribution=a)
    assert plain.spend == shifted.spend == Decimal("9100.00")
    assert plain.net == shifted.net
    assert sum(m.spend for m in plain.months) == sum(m.spend for m in shifted.months)
    # And the category grid agrees with the month buckets, both ways.
    for result in (plain, shifted):
        by_month = {m.month: m.spend for m in result.months}
        grid: dict[str, Decimal] = {}
        for series in result.category_months:
            for name, value in zip([m.month for m in result.months], series.amounts):
                grid[name] = grid.get(name, Decimal(0)) + value
        assert grid == by_month


def test_a_bill_paid_late_in_the_month_is_not_a_settlement():
    """`before_day` is the whole discrimination: a card payment on the 25th is
    this month's, and shifting it would be worse than not shifting at all."""
    rows = [split("withdrawal", "9000.00", category="Credit Card", when="2026-08-25")]
    result = service.ledger_analysis(
        rows, rules=CARD_RULES,
        attribution=service.Attribution(categories=frozenset({"Credit Card"})),
    )
    assert {m.month: m.spend for m in result.months} == {"2026-08": Decimal("9000.00")}


def test_a_keep_larger_than_the_bill_cannot_invent_money():
    rows = [
        split("withdrawal", "500.00", category="Credit Card",
              when="2026-08-05", external_id="x"),
    ]
    result = service.ledger_analysis(
        rows, rules=CARD_RULES,
        attribution=service.Attribution(
            categories=frozenset({"Credit Card"}), keep={"x": Decimal("99999")}
        ),
    )
    assert sum(m.spend for m in result.months) == Decimal("500.00")


def test_configuring_nothing_changes_nothing():
    """The default has to be exactly today's behaviour, or every existing
    figure moves the day this ships."""
    rows = [
        split("withdrawal", "9000.00", category="Credit Card", when="2026-08-05"),
        split("deposit", "900.00", category="Salary", when="2026-08-09"),
    ]
    assert service.ledger_analysis(rows, rules=CARD_RULES) == service.ledger_analysis(
        rows, rules=CARD_RULES, attribution=service.Attribution()
    )


CARD_RULES = {
    "rules": [{"category": "Credit Card"}, {"category": "Shopping"}, {"category": "Salary"}],
    "not_spend": [],
}


def test_a_shift_never_creates_a_month_the_window_excludes():
    """§73. `/analysis` filters splits to the window and only then calls this,
    so a shift that mints its own bucket grows a column for a month the range
    picker says is excluded — seeded by one settled bill and nothing else.

    When the target is outside, the money stays where it was paid: visibly in
    the wrong month beats invisibly in a month you did not ask for. The total
    is unchanged either way, which is the invariant that matters.
    """
    from datetime import date as _date

    rows = [split("withdrawal", "9000.00", category="Credit Card", when="2026-08-05")]
    a = service.Attribution(categories=frozenset({"Credit Card"}))

    # A window covering August alone: July is not in scope, so nothing moves.
    august = service.ledger_analysis(
        rows, rules=CARD_RULES, attribution=a,
        coverage=(_date(2026, 8, 1), _date(2026, 8, 31)),
    )
    assert {m.month for m in august.months} == {"2026-08"}
    assert sum(m.spend for m in august.months) == Decimal("9000.00")

    # A window that reaches into July: the shift happens.
    both = service.ledger_analysis(
        rows, rules=CARD_RULES, attribution=a,
        coverage=(_date(2026, 7, 1), _date(2026, 8, 31)),
    )
    assert {m.month for m in both.months} == {"2026-07"}
    assert sum(m.spend for m in both.months) == Decimal("9000.00")


def test_a_settlement_is_dated_to_the_anchor_day_in_either_month():
    """§94. > "always put credit card on 22 of the month whether current or
    previous"

    A bill is not spent on the day it is paid; it is the month's card activity,
    and the anchor is the statement date. The settled part lands on the 22nd of
    the previous month and the kept part on the 22nd of this one.
    """
    from datetime import date as _date

    a = service.Attribution(categories=frozenset({"Credit Card"}), to_day=22)
    paid = _date(2026, 8, 5)

    # The kept part: 22nd of the month it was paid in, not the 5th.
    assert a.anchor("Credit Card", paid) == _date(2026, 8, 22)
    # A category that does not settle is untouched.
    assert a.anchor("Shopping", paid) == paid
    # A payment after `before_day` is not a settlement, so it keeps its date.
    assert a.anchor("Credit Card", _date(2026, 8, 25)) == _date(2026, 8, 25)

    # And February, where the 22nd exists but the naive `replace(day=…)` on a
    # longer `to_day` would not.
    late = service.Attribution(categories=frozenset({"Credit Card"}), to_day=28)
    assert late.anchor("Credit Card", _date(2026, 2, 3)) == _date(2026, 2, 28)
