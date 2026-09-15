"""All narration grammars. SPEC §6.5, §10.

Every string here is synthetic. Nothing in this file comes from a real
statement — the shapes are real, the values are invented.
"""

from datetime import date, time
from decimal import Decimal

import pytest

from passbook.models import CHG, IMPS, INT, NEFT, OTHER, SCHEME, UPI, Transaction
from passbook.narration import backfill_reversal_payees, parse, strip_trailing_timestamp

# --- 1. UPI debit -------------------------------------------------------------
UPI_DR = (
    "UPI/DR/412345678901/ZOKVEX QI/YESB/**12345@YBL/UPI//"
    "AXI62CB1234567890E9BD6542D4A7523C8/09/05/2026 01:51:33"
)
# --- 1b. UPI credit (14 rows in the reference statement; SPEC §6.5 omitted it)
UPI_CR = (
    "UPI/CR/412345678902/MURZAB QO/SBIN/**NIRAJ@OKSBI/PAY//"
    "SBIA1234AAB98A12C3ED9AA66B8B5678/16/05/2026 19:05:41"
)
# --- 1c. UPI reference (1 row; also absent from SPEC §6.5) --------------------
UPI_REF = (
    "UPI/REF/412345678903/QILGRU ZA/AXIS/**HJAIN@OKAXIS/PAY/"
    "PTM1234B1FF567B8C97CA5CF9E/12/07/2026 18:11:42/998877"
)
# --- 1d. UPI, UTR first (§97) -------------------------------------------------
# The same six facts as 1a in a different ORDER: the UTR at index 1 and the
# direction at index 2, where the reference bank puts them at 2 and 1. Only the
# prefix tells the two apart, which is why it is matched exactly.
UPI_UTR_FIRST = "UPIAR/412345678904/DR/ZOKVEX QI/**12345@YBL"
# --- 1e. UPI, behind the bank's own words (§99) -------------------------------
# The reference bank's layout with the transaction type printed in front of it.
# Seven segments, unanimous across a real 72-row statement.
UPI_PREFIXED = (
    "WDL TFR UPI/DR/512345678901/ZOKVEX QI/YESB/9990001111/"
    "Sent 1234567890123 AT 04100 EXAMPLE NAGAR, PLACE"
)
UPI_PREFIXED_CR = (
    "DEP TFR UPI/CR/512345678902/MURZAB QO/UTIB/murzab@okaxis/"
    "Sent 1234567890124 AT 04100 EXAMPLE NAGAR, PLACE"
)
# --- 2. UPI reversal ----------------------------------------------------------
UPI_REV = "UPI/412345678901/R01/06/08/2026"
# --- 3. IMPS credit -----------------------------------------------------------
IMPS_CR = (
    "INET-IMPS-CR/M2I IMPS P/ICICI BANK/123456789012/9876543210/"
    "9876543210/12/05/2026 05:07:45/123456789012"
)
# --- 4. NEFT credit -----------------------------------------------------------
NEFT_CR = (
    "NEFT CR-HDFCH25142509791-HDFC0000060-ZOKVEX QILGRU MURZAB LIMITED--"
    "3000-NEFT    5013 THOKVA ZE 51-NEFT"
)
# --- 5-8. plain-text shapes ---------------------------------------------------
SCHEME_DR = "PMSBY RENEWAL(26-27) - 888800011 - 1234567890123"
SMS_CHG = "SMS CHARGES ON ACTUAL BASIS"
INTEREST = "SBINT FOR THE PERIOD FROM27-MAR-26 TO 27-JUN-26"
CARD_CHG = "DEBIT CARD ANNUAL CHARGES XXXXXXXXXXX1234"


def test_upi_debit():
    got = parse(UPI_DR)
    assert got["channel"] == UPI
    assert got["utr"] == "412345678901"
    assert got["payee"] == "ZOKVEX QI"
    assert got["counterparty_bank"] == "YBL"
    assert got["is_reversal"] is False


def test_upi_credit_shares_the_debit_layout():
    got = parse(UPI_CR)
    assert got["channel"] == UPI
    assert got["payee"] == "MURZAB QO"
    assert got["utr"] == "412345678902"
    assert got["counterparty_bank"] == "OKSBI"


def test_upi_reference():
    got = parse(UPI_REF)
    assert got["channel"] == UPI
    assert got["payee"] == "QILGRU ZA"
    assert got["counterparty_bank"] == "OKAXIS"


def test_upi_reversal_sets_flag_and_keeps_utr():
    got = parse(UPI_REV)
    assert got["channel"] == UPI
    assert got["is_reversal"] is True
    # The UTR is what links a reversal back to its original debit. SPEC §6.5.
    assert got["utr"] == "412345678901"
    assert got["utr"] == parse(UPI_DR)["utr"]


def test_upi_with_the_utr_first_is_read_in_its_own_order(): 
    """§97. Positional parsing cannot tell two orders apart from a token count,
    so the prefix decides — `UPIAR` rather than a bare `UPI`. Reading it with
    the reference bank's order would report the direction as the UTR."""
    got = parse(UPI_UTR_FIRST)
    assert got["channel"] == UPI
    assert got["utr"] == "412345678904"
    assert got["payee"] == "ZOKVEX QI"
    assert got["counterparty_bank"] == "YBL"


def test_upi_behind_the_banks_own_words():
    """§99. One bank prints its transaction type before the channel, so the
    narration starts `WDL TFR UPI/` and not `UPI/`. Measured: that rejected all
    72 rows of a real statement and every payee came out unparsed."""
    got = parse(UPI_PREFIXED)
    assert got["channel"] == UPI
    assert got["utr"] == "512345678901"
    assert got["payee"] == "ZOKVEX QI"
    # A four-letter token there is an IFSC's bank prefix.
    assert got["counterparty_bank"] == "YESB"


def test_upi_behind_the_banks_own_words_reads_a_credit_the_same_way():
    got = parse(UPI_PREFIXED_CR)
    assert got["channel"] == UPI
    assert got["payee"] == "MURZAB QO"
    assert got["counterparty_bank"] == "UTIB"


def test_a_prefixed_upi_falls_back_to_the_vpa_handle_for_the_bank():
    """Six rows of the measured file carry two words where the others carry a
    bank code. Two words are a name that has run on, not a bank — so the handle
    is used instead of printing half a payee as a counterparty."""
    got = parse(
        "WDL TFR UPI/DR/512345678903/QILGRU ZA/SOME NAME/**12345@OKHDFCBANK/Sent"
    )
    assert got["payee"] == "QILGRU ZA"
    assert got["counterparty_bank"] == "OKHDFCBANK"


def test_the_three_upi_orders_cannot_match_each_others_narrations():
    """They are told apart by their FIRST segment and nothing else, so the one
    thing that must hold is that each rejects the other two. A matcher that
    accepted a neighbour's layout would read the direction as a UTR and file a
    payee under a bank code — silently, and only for that bank."""
    from passbook.narration import _upi_prefixed, _upi_transfer, _upi_utr_first

    for matcher, mine in (
        (_upi_transfer, UPI_DR),
        (_upi_utr_first, UPI_UTR_FIRST),
        (_upi_prefixed, UPI_PREFIXED),
    ):
        assert matcher(mine) is not None, matcher.__name__
        for theirs in (UPI_DR, UPI_UTR_FIRST, UPI_PREFIXED):
            if theirs is mine:
                continue
            assert matcher(theirs) is None, f"{matcher.__name__} claimed {theirs[:20]!r}"


# --- §106 the grammars the second bank needed ---------------------------------

INTEREST_PREFIXED = "999900001111:Int.Pd:01-01-2024 to 31-03-2024"
MINBAL_CHGS = "SB MINBAL CHGS"
LONE_REF = "AA4826270"
SHORT_PHRASE = "NEW ACCOUNT OPEN 1"


def test_interest_is_matched_by_its_marker_not_by_a_prefix():
    """One bank writes `SBINT …` and another `<account>:Int.Pd:<range>`. The
    second puts the account number in front, so a prefix test cannot see it."""
    got = parse(INTEREST_PREFIXED)
    assert got["channel"] == INT
    assert got["payee"] == "Savings Interest"


def test_interest_never_carries_the_account_number_into_the_payee():
    """§11, and the same reasoning `_scheme` uses for the customer ID inside a
    PMSBY line: a payee is a display string that reaches the ledger, a payee
    report and a log line. Nothing is extracted from the body."""
    got = parse(INTEREST_PREFIXED)
    assert "999900001111" not in str(got["payee"])
    assert all("999900001111" not in str(v) for v in got.values())


def test_an_abbreviated_charge_is_still_a_charge():
    got = parse(MINBAL_CHGS)
    assert got["channel"] == CHG
    assert got["payee"] == "Bank Charges"


def test_chgs_matches_as_a_word_and_not_as_a_fragment():
    """A boundary on both, so a token that merely contains the letters is not a
    charge. This matcher runs last, but "last" is not "anything"."""
    from passbook.narration import _charges

    assert _charges("SB MINBAL CHGS") is not None
    assert _charges("CHGSMITH ENTERPRISES") is None


def test_a_narration_that_is_only_a_reference_becomes_that_reference():
    """§106. Not a guess about what the code means — an observation that there
    is nothing else in the line. It matters because `payee_inventory` collapses
    every payee-less row into one `(unparsed)` heading, and a recurring debit in
    there cannot be aliased or categorised at all."""
    got = parse(LONE_REF)
    assert got["payee"] == LONE_REF
    assert got["channel"] == OTHER


def test_a_short_phrase_with_no_separators_counts_too():
    got = parse(SHORT_PHRASE)
    assert got["payee"] == SHORT_PHRASE


def test_the_lone_token_matcher_claims_nothing_a_grammar_could_have():
    """It is last for a reason. Anything with a separator in it belongs to a
    positional grammar, whether or not one matched — claiming it here would put
    a whole UPI narration on the Payees page as a payee name."""
    from passbook.narration import _lone_token

    for raw in (UPI_DR, UPI_CR, UPI_REF, UPI_REV, IMPS_CR, NEFT_CR, UPI_PREFIXED):
        assert _lone_token(raw) is None, raw[:30]


def test_a_long_narration_is_not_a_payee():
    """The flood guard. A per-row narration claimed this way would put one token
    per transaction on the Payees page, which is the opposite of useful."""
    from passbook.narration import _lone_token

    assert _lone_token("A" * 41) is None


def test_imps_credit():
    got = parse(IMPS_CR)
    assert got["channel"] == IMPS
    assert got["payee"] == "M2I IMPS P"
    assert got["counterparty_bank"] == "ICICI BANK"


def test_neft_credit_payee_is_not_truncated():
    got = parse(NEFT_CR)
    assert got["channel"] == NEFT
    assert got["payee"] == "ZOKVEX QILGRU MURZAB LIMITED"
    assert len(got["payee"]) > 10  # unlike UPI, NEFT carries the full name
    assert got["counterparty_bank"] == "HDFC"


def test_scheme_does_not_leak_the_customer_id():
    got = parse(SCHEME_DR)
    assert got["channel"] == SCHEME
    # The customer ID is also the PDF statement password. It must not end up in
    # a payee, a ledger description, or a `passbook payees` report. SPEC §11.
    assert got["payee"] == "PMSBY"
    assert "888800011" not in str(got)


def test_sms_charges():
    assert parse(SMS_CHG)["channel"] == CHG


def test_card_charges_do_not_leak_the_last_four():
    got = parse(CARD_CHG)
    assert got["channel"] == CHG
    assert "1234" not in str(got)


def test_savings_interest():
    # Note the missing space after FROM — that is the bank's formatting.
    assert parse(INTEREST)["channel"] == INT


def test_unrecognised_falls_through_to_other_without_raising():
    got = parse("SOMETHING ENTIRELY NEW FROM THE BANK 2029")
    assert got["channel"] == OTHER
    assert got["payee"] is None


@pytest.mark.parametrize("text", ["", "   ", "/", "///", "UPI/", "UPI/DR/"])
def test_never_raises_on_degenerate_input(text):
    assert parse(text)["channel"] in (UPI, OTHER)


# --- the four gotchas from SPEC §6.5 -----------------------------------------


def test_trailing_timestamp_is_stripped_before_tokenising():
    """The single most important line in narration.py.

    `.../09/05/2026 01:51:33` adds four spurious tokens to a naive split('/'),
    which pushes the payee off by one.
    """
    assert strip_trailing_timestamp(UPI_DR).endswith("D4A7523C8")
    assert "2026" not in strip_trailing_timestamp(UPI_DR).split("/")[-1]
    # Without the strip, token 3 would still be the payee but the tail would be
    # polluted; assert the parse survives the timestamp entirely.
    assert parse(UPI_DR)["payee"] == "ZOKVEX QI"


def test_date_only_trailing_timestamp_also_stripped():
    assert strip_trailing_timestamp("UPI/DR/1/AB/YESB/**1@YBL/UPI//X/06/08/2026") == (
        "UPI/DR/1/AB/YESB/**1@YBL/UPI//X"
    )


def test_masked_vpa_yields_only_the_handle():
    """`**39725@YBL` — the VPA is masked by the bank, so only the handle is
    usable. SPEC §6.5."""
    assert parse(UPI_DR)["counterparty_bank"] == "YBL"


def test_truncated_payee_is_left_alone():
    """Canara truncates to ~10 chars; reconstruction is not attempted. D10."""
    payee = parse(UPI_DR)["payee"]
    assert len(payee) <= 10
    assert payee == "ZOKVEX QI"  # not expanded, not stripped further


# --- txn_time: stripped for tokenising, but not discarded. SPEC §6.5 ---------


def test_time_of_day_is_captured_from_a_trailing_timestamp():
    assert parse(UPI_DR)["txn_time"] == time(1, 51, 33)
    assert parse(UPI_CR)["txn_time"] == time(19, 5, 41)


def test_time_is_captured_even_when_the_timestamp_is_not_trailing():
    """`UPI/REF/` and `INET-IMPS-` put a reference AFTER the timestamp, so the
    anchored stripping pattern never fires on them. Extraction must search."""
    assert parse(UPI_REF)["txn_time"] == time(18, 11, 42)
    assert parse(IMPS_CR)["txn_time"] == time(5, 7, 45)
    # and the strip genuinely does not fire on those shapes
    assert strip_trailing_timestamp(UPI_REF) == UPI_REF


def test_capturing_the_time_does_not_disturb_the_payee():
    """The whole point: strip before tokenising, but keep the value."""
    assert parse(UPI_DR)["payee"] == "ZOKVEX QI"
    assert parse(UPI_REF)["payee"] == "QILGRU ZA"


@pytest.mark.parametrize("text", [NEFT_CR, SMS_CHG, INTEREST, CARD_CHG, SCHEME_DR, UPI_REV])
def test_narrations_without_a_clock_yield_no_time(text):
    """The R01 reversal carries a date but no time."""
    assert parse(text)["txn_time"] is None


def test_an_impossible_clock_is_ignored_rather_than_raised():
    assert parse("UPI/DR/1/AB/YESB/**1@YBL/UPI//X/09/05/2026 99:99:99")["txn_time"] is None


# --- reversal payee backfill by UTR. SPEC §6.5 --------------------------------


def test_reversal_payee_is_backfilled_from_the_matching_utr():
    debit = Transaction(
        txn_id="20260509000001", txn_date=date(2026, 5, 9), narration=UPI_DR,
        debit=Decimal("48.00"), credit=None, balance=Decimal("100.00"),
        **parse(UPI_DR),
    )
    reversal = Transaction(
        txn_id="20260807000002", txn_date=date(2026, 8, 7), narration=UPI_REV,
        debit=None, credit=Decimal("48.00"), balance=Decimal("148.00"),
        **parse(UPI_REV),
    )
    assert reversal.payee is None
    assert backfill_reversal_payees([debit, reversal]) == 1
    assert reversal.payee == "ZOKVEX QI"
    assert reversal.counterparty_bank == "YBL"
    assert reversal.is_reversal is True
    assert debit.payee == "ZOKVEX QI"  # the original is untouched


def test_backfill_leaves_an_unmatched_reversal_alone():
    reversal = Transaction(
        txn_id="20260807000002", txn_date=date(2026, 8, 7), narration=UPI_REV,
        debit=None, credit=Decimal("48.00"), balance=Decimal("148.00"),
        **parse(UPI_REV),
    )
    assert backfill_reversal_payees([reversal]) == 0
    assert reversal.payee is None


def test_backfill_never_overwrites_an_existing_payee():
    debit = Transaction(
        txn_id="1", txn_date=date(2026, 5, 9), narration=UPI_DR,
        debit=Decimal("1"), credit=None, balance=Decimal("1"), **parse(UPI_DR),
    )
    reversal = Transaction(
        txn_id="2", txn_date=date(2026, 8, 7), narration=UPI_REV,
        debit=None, credit=Decimal("1"), balance=Decimal("2"), **parse(UPI_REV),
    )
    reversal.payee = "ALREADY SET"
    assert backfill_reversal_payees([debit, reversal]) == 0
    assert reversal.payee == "ALREADY SET"


def test_direction_is_not_taken_from_the_narration():
    """The Withdrawals/Deposits columns are authoritative. SPEC §6.5.

    narration.parse returns no debit/credit at all, by design.
    """
    assert "debit" not in parse(UPI_DR)
    assert "credit" not in parse(UPI_CR)
