# Bank profiles that ship with passbook

**SPEC §55.** A profile is six column names and a date format. No code — which
is why these can ship, and why a new install can read a bank nobody on this
machine has ever seen.

`config/banks/*.yaml` on the operator's machine loads **on top** of these and
wins on a name clash, so a shipped profile is a good default and never a
constraint. When a bank re-skins its export, the operator fixes it once on
*Add a bank* and their version takes over.

## What a shipped profile is, and is not

**It is a layout, not a bank.** Net banking, the mobile app and a branch
printout are three different layouts from one bank, and a bank can change any
of them without warning. So a name here means "this layout, last time anyone
looked".

**It is never trusted.** The balance-continuity invariant runs on every import
whatever profile matched, so a stale profile refuses loudly rather than
importing something wrong. That is the only reason shipping defaults is safe.

## Adding one

You need four things, and **none of them is a statement**:

1. the six column headings, read off the top of the table;
2. the label printed beside the account number;
3. the date format;
4. whether the bank prints a per-row reference at all.

`scripts/probe.py <statement>` prints the whole document as coordinates and
token shapes — `92-92-94@50.0-91.9` — and audits its own output, refusing to
print if a single word of the source survives. Anyone can generate that from
their own file and hand it over; it names nobody and no amount.

Then add the YAML here and a case to `tests/test_builtin_banks.py`, which loads
every shipped profile and checks it parses, names only real fields, and does not
collide with another bank.
