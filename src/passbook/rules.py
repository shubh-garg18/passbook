"""The categorisation rules, and where they are read from. DECISIONS.md §36.

These used to live beside the code that pushed them to a separate application's
rules engine. They are here now because passbook applies them itself, at the
moment a row is written, and there is no engine to push to.

That is a smaller change than it sounds. `service.predict_category` and
`service.predict_tags` already implemented these rules, because §24 had to know
what a row *should* say in order to report that it did not. The mirror is now
the original.

**`config/rules.yaml` is the only place the operator's lists are stated.** Not
this docstring, not the README, not a comment. `not_spend` in particular has
changed several times, and every document that wrote the list down was wrong
within a release — so read the file, never quote it.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from .yamlfile import read_yaml

RULES_FILE = Path("config/rules.yaml")
BILLS_FILE = Path("config/bills.yaml")


def load_rules(path: Path | None = None) -> dict:
    """The rules file, or an empty mapping.

    A missing file is not an error: a fresh clone has no rules yet, and every
    caller treats "no rules" as "predict nothing", which is the honest answer
    rather than a guessed one.
    """
    path = path or RULES_FILE
    if not path.exists():
        return {}
    return read_yaml(path)


def load_bills(path: Path | None = None) -> list[dict]:
    """`bills.yaml` ships empty.

    On the reference statement nothing met the recurrence test — at least three
    occurrences with a median gap of 25 to 35 days — so there is nothing to
    create and no placeholder is invented.
    """
    path = path or BILLS_FILE
    if not path.exists():
        return []
    loaded = read_yaml(path)
    return loaded.get("bills") or []


def rule_categories(rules: dict | None = None) -> dict[str, str]:
    """display-name -> category, inverted out of rules.yaml.

    Rules match on the *display* name (alias where one exists, raw token
    otherwise), because that is what `description` carries at push time.
    """
    rules = rules if rules is not None else load_rules()
    mapping: dict[str, str] = {}
    for spec in rules.get("rules") or []:
        category = spec.get("category")
        if not category:
            continue
        for payee in spec.get("payees") or []:
            mapping[payee] = category
    return mapping


def predict_category(description: str, narration: str, rules: dict | None = None) -> str:
    """The category this row should carry, applied when the row is written.

    Inverting the `payees:` lists alone is not enough, and getting that wrong
    made the re-apply preview claim rows would *lose* their category:

    * `description_starts` is a PREFIX match, so `Mother` also catches
      `Mother (via friend)`.
    * Several rules match the raw narration instead — `Bank Charges` via
      `notes_contains: CHARGES`, `Interest Income` via `notes_starts: SBINT`,
      `Credit Card` via `notes_contains: **TCARD`. Those have no payee entry at
      all.
    * Every categorisation rule sets `stop_processing: false`, so all matching
      rules run and the LAST one wins.
    """
    rules = rules if rules is not None else load_rules()
    found = ""
    for spec in rules.get("rules") or []:
        category = spec.get("category")
        if not category:
            continue
        matched = any(description.startswith(p) for p in (spec.get("payees") or []))
        if not matched and spec.get("notes_contains"):
            matched = spec["notes_contains"] in narration
        if not matched and spec.get("notes_starts"):
            matched = narration.startswith(spec["notes_starts"])
        if matched:
            found = category
    return found


# Tags a payee edit can move, and therefore the only tags an in-place sync is
# allowed to write. SPEC §23.2.
#
# Deliberately NOT the whole tag vocabulary:
#
#   * `reversal` is set by the pusher from a parser-derived fact (§7.2). Config
#     cannot change it, so a sync must never touch it.
#   * `large-oneoff` is set once, when the row is written, by
#     `predict_large_oneoff` — which can finally honour `exclude_categories`,
#     because passbook decides the category before it stores the row rather
#     than after. It is not in this set because a sync must not move it: the
#     threshold is a setting, and re-tagging history when a setting changes is
#     a different decision from following a rename.
#
# Everything outside this set is carried through a sync untouched.


def managed_tags(rules: dict | None = None) -> set[str]:
    """The tags derived from `rules.yaml` that a rename or re-categorisation moves."""
    rules = rules if rules is not None else load_rules()
    tags = {str(spec["tag"]) for spec in (rules.get("rules") or []) if spec.get("tag")}
    not_earnings = (rules.get("not_earnings") or {}).get("tag")
    if not_earnings:
        tags.add(str(not_earnings))
    return tags


def predict_tags(
    description: str, narration: str, kind: str, rules: dict | None = None
) -> set[str]:
    """The managed tags this row should carry.

    Two sources, both read straight out of `rules.yaml`:

    * a category rule's own `tag:` (`food`, `family`). `add_tag` is additive and
      every rule sets `stop_processing: false`, so **every** match contributes —
      unlike the category, where the last match wins.
    * `not_earnings`, which is inverted: a deposit carries the tag unless its
      description starts with one of `earnings_only`. That is the strict rule in
      §8.1, and it can never land on a withdrawal.

    This exists because `add_tag` cannot un-tag. Renaming a payee into an
    earnings source leaves the stale `not-earnings` tag behind, and a stale
    `not-earnings` is not a cosmetic problem: non-negotiable 9 excludes those
    deposits from earnings, so the total silently reads low.
    """
    rules = rules if rules is not None else load_rules()
    tags: set[str] = set()

    for spec in rules.get("rules") or []:
        if not spec.get("tag"):
            continue
        matched = any(description.startswith(p) for p in (spec.get("payees") or []))
        if not matched and spec.get("notes_contains"):
            matched = spec["notes_contains"] in narration
        if not matched and spec.get("notes_starts"):
            matched = narration.startswith(spec["notes_starts"])
        if matched:
            tags.add(str(spec["tag"]))

    not_earnings = rules.get("not_earnings") or {}
    if kind == "deposit" and not_earnings.get("tag"):
        earnings = [str(p) for p in (not_earnings.get("earnings_only") or [])]
        if not any(description.startswith(p) for p in earnings):
            tags.add(str(not_earnings["tag"]))

    return tags


def predict_large_oneoff(
    kind: str,
    amount,
    description: str,
    narration: str,
    category: str,
    threshold,
    rules: dict | None = None,
) -> str:
    """The `large-oneoff` tag, or `""`. DECISIONS.md §36.

    A withdrawal over the threshold that is none of the excluded things. All
    four conditions must hold — it is a strict rule, as it always was.

    **`exclude_categories` finally works.** It was documented as inert, because
    the engine that applied it ran at store time and the category was not
    committed yet, so the two rows the exclusion existed for got tagged anyway.
    passbook decides the category before it writes the row, so the exclusion is
    evaluated against the category the row is about to have.
    """
    if kind != "withdrawal":
        return ""
    spec = (rules if rules is not None else load_rules()).get("large_oneoff") or {}
    tag = str(spec.get("tag") or "")
    if not tag or Decimal(str(amount)) <= Decimal(str(threshold)):
        return ""
    if any(description.startswith(str(p)) for p in (spec.get("exclude_payees") or [])):
        return ""
    if any(str(f) in narration for f in (spec.get("exclude_notes") or [])):
        return ""
    if category and category in {str(c) for c in (spec.get("exclude_categories") or [])}:
        return ""
    return tag
