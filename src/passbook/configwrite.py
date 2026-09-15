"""Edit config/*.yaml in place, preserving comments. SPEC §14.

`rules.yaml`'s comments carry knowledge that cost real money to acquire —
`SATBEER S  # a KFC franchise, not a person`, and the other three tokens D10
records as misread. Plain PyYAML round-trips destroy every one of them, so
this uses ruamel.yaml, which preserves comments, key order and formatting.

Nothing here writes without the caller having shown a diff first. `plan_*`
produces the new text, `apply` writes it — deliberately two steps.
"""

import copy
import difflib
import io
from dataclasses import dataclass
from decimal import Decimal  # noqa: F401  — used in an annotation
from pathlib import Path

from ruamel.yaml import YAML

from .config import ATTRIBUTION_FILE, PAYEE_ALIASES
from .rules import RULES_FILE

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.width = 4096  # never re-wrap a long comment into nonsense


@dataclass
class ConfigChange:
    path: Path
    before: str
    after: str

    @property
    def changed(self) -> bool:
        return self.before != self.after

    def diff(self) -> str:
        return "".join(
            difflib.unified_diff(
                self.before.splitlines(keepends=True),
                self.after.splitlines(keepends=True),
                fromfile=f"a/{self.path}",
                tofile=f"b/{self.path}",
                n=3,
            )
        )

    def apply(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.after, encoding="utf-8")


#: How far a comment line may be indented and still count as its own block
#: rather than the wrapped continuation of the line above. A sequence item sits
#: at 4 and its inline comment far to the right; a section header sits at 0.
_BLOCK_INDENT = 6


def _split_trailing_comment(seq, index: int) -> None:
    """Move a comment *block* that follows item `index` to the end of `seq`.

    ruamel stores an item's end-of-line comment and everything that follows it
    — blank lines, the next section's header — in one token attached to that
    item. Appending therefore renders the new entry *after* the next section's
    header, which is how `- KFC` ended up under `# ── other spending ──` while
    still belonging to the Eating out list.

    That is worse than untidy. These comments are the D10 evidence for why a
    token was categorised; a comment pointing at the wrong entry is actively
    misleading. Split at the first newline: the inline part stays with the item
    it describes, the block after it moves to the end.
    """
    ca = getattr(seq, "ca", None)
    if ca is None or index not in ca.items:
        return
    token = ca.items[index][0]
    if token is None:
        return

    # **Split at the first line that is its own block, not at the first
    # newline.** SPEC §112.1. ruamel hands over one token holding all of it:
    #
    #     # alias of PAYONEER INC OPGSP...; a payee of Salary, so   <- inline
    #                 # excluding it contradicted its own …         <- a WRAP
    #                                                               <- blank
    #     # ── what is not spending ──                              <- a block
    #
    # Splitting on the first newline tore the annotation in half and gave the
    # second line to the newly appended item — a comment pointing at the wrong
    # entry, and here at one it contradicts. That is the exact failure this
    # function exists to prevent, one level down, and it was found by using it.
    #
    # Indentation separates them: a wrap is aligned into the comment column of
    # the line above, a block starts at the sequence's own indent or at 0.
    lines = token.value.splitlines(keepends=True)
    cut = None
    for position, line in enumerate(lines[1:], 1):
        if line.strip() and (len(line) - len(line.lstrip())) <= _BLOCK_INDENT:
            cut = position
            break
    if cut is None:
        return  # an inline comment and its wraps; all of it belongs where it is

    # Take the blank lines before the block with it, so the spacing that
    # separated it from the list is the spacing that separates it from the list.
    while cut > 1 and not lines[cut - 1].strip():
        cut -= 1

    token.value = "".join(lines[:cut])
    moved = copy.copy(token)
    # A leading newline, or ruamel renders the block as the new item's INLINE
    # comment and a section header ends up on the same line as an entry.
    moved.value = "\n" + "".join(lines[cut:])
    last = len(seq) - 1
    ca.items.setdefault(last, [None, None, None, None])[0] = moved


def append_to_seq(seq, value) -> None:
    """Append, keeping any trailing comment block after the new item."""
    previous_last = len(seq) - 1
    seq.append(value)
    if previous_last >= 0:
        _split_trailing_comment(seq, previous_last)


def normalise_comments(data) -> None:
    """Repair entries appended before append_to_seq existed.

    Walks every sequence and moves a stray block comment off a non-final item
    onto the last one, which puts previously-appended entries back inside the
    list they actually belong to.
    """
    if isinstance(data, dict):
        for value in data.values():
            normalise_comments(value)
    elif isinstance(data, list):
        for item in data:
            normalise_comments(item)
        for index in range(len(data) - 1):
            _split_trailing_comment(data, index)


def _dump(data) -> str:
    buf = io.StringIO()
    _yaml.dump(data, buf)
    return buf.getvalue()


def _load(path: Path):
    if path.exists():
        return _yaml.load(path.read_text(encoding="utf-8")) or {}
    return {}


def plan_aliases(new: dict[str, str], path: Path | None = None) -> ConfigChange:
    """Set or clear aliases. An empty value removes the entry."""
    path = path or PAYEE_ALIASES
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    data = _load(path)
    if "aliases" not in data or data["aliases"] is None:
        data["aliases"] = {}
    aliases = data["aliases"]

    for token, alias in new.items():
        alias = (alias or "").strip()
        if alias:
            aliases[token] = alias
        elif token in aliases:
            del aliases[token]

    return ConfigChange(path=path, before=before, after=_dump(data))


def plan_categories(
    assignments: dict[str, str],
    aliases: dict[str, str],
    path: Path | None = None,
    *,
    renames: dict[str, str] | None = None,
    tags: dict[str, str] | None = None,
) -> ConfigChange:
    """Assign tokens to categories in rules.yaml, following any payee renames.

    `renames` is old display name -> new display name, and is applied FIRST so
    an explicit category chosen in the same submission overwrites it rather than
    racing it. It lives here rather than in a plan of its own because two plans
    over one file do not compose: each would diff against the text on disk, and
    applying both would leave whichever ran last.

    `assignments` is raw token -> category name. Two things make this less
    obvious than it looks:

    * **Rules match the display name, not the token.** `description` is pushed
      as "<alias or token> (<channel>)", so a token that has an alias must be
      listed under its alias, or the rule never fires.
    * **Aliases collapse.** Two tokens sharing one alias (a vendor with two QR
      codes) must produce a single entry, not a duplicate.

    A token is removed from any category it no longer belongs to, so
    re-assigning moves it rather than listing it twice.
    """
    path = path or RULES_FILE
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    data = _load(path)
    rules = data.get("rules")
    if rules is None:
        return ConfigChange(path=path, before=before, after=before)

    if renames:
        _follow_renames(rules, renames)

    # Give a category a roll-up tag. SPEC §33: moving three `food` categories
    # into an untagged one dropped the tag from 29 rows and under-reported food
    # spend, silently. This is how the operator says "and it is still
    # food" in the same action, instead of discovering it weeks later.
    for category, tag in (tags or {}).items():
        spec = next((r for r in rules if str(r.get("category", "")) == category), None)
        if spec is None:
            raise KeyError(f"no rule in {path} has category {category!r}")
        if tag:
            spec["tag"] = tag
        else:
            spec.pop("tag", None)

    by_category = {spec.get("category"): spec for spec in rules if spec.get("category")}

    for token, category in assignments.items():
        category = (category or "").strip()
        display = (aliases.get(token) or token).strip()

        # Drop the display name from wherever it currently sits.
        for spec in rules:
            payees = spec.get("payees")
            if payees and display in payees and spec.get("category") != category:
                payees.remove(display)

        if not category:
            continue
        spec = by_category.get(category)
        if spec is None:
            # Unknown category: refuse rather than invent a rule shape. The
            # operator adds the rule; the UI only fills in its payees.
            raise KeyError(
                f"no rule in {path} has category {category!r}. "
                f"Known: {sorted(k for k in by_category if k)}"
            )
        payees = spec.get("payees")
        if payees is None:
            spec["payees"] = payees = []
        if display not in payees:
            append_to_seq(payees, display)

    return ConfigChange(path=path, before=before, after=_dump(data))


def _follow_renames(rules, renames: dict[str, str]) -> None:
    """Follow a payee rename through the rule payee lists. SPEC §23.4.

    Rules match the **display name** — `description_starts: Canteen` against a
    description pushed as `Canteen (UPI)`. So renaming an alias moves the
    description out from under its own rule, and the row silently loses the
    category the operator had already chosen for it. Measured: alias
    `Canteen` -> `Mess` left `payees: [Canteen]` in place, and
    `predict_category('Mess (UPI)')` returned `''`.

    This is not D10's "never invent a category". Nothing is inferred: the
    classification already exists and the operator has only relabelled the thing
    it is attached to. Dropping it would discard a decision, not withhold a
    guess.

    Rewritten in place, so the entry keeps its position and — more to the point
    — its comment, which is where D10's evidence lives.
    """
    for spec in rules:
        payees = spec.get("payees")
        if not payees:
            continue
        for index, payee in enumerate(payees):
            renamed = renames.get(str(payee))
            # A rename onto a name already in the list would duplicate the
            # entry. Leave it; the loop below de-duplicates on assignment.
            if renamed and renamed not in payees:
                payees[index] = renamed


def plan_new_category(name: str, path: Path | None = None) -> ConfigChange:
    """Add an empty category rule to `rules.yaml`. SPEC §26.

    **This is not D10 being relaxed.** D10 forbids *inferring* a category from a
    ten-character fragment — guessing that `THE CASUA` is casual dining when it
    is a clothing store. Creating a category the operator has typed infers
    nothing at all; it records a decision they have already made, and until now
    the only way to record it was to hand-edit a YAML file, which is why the
    dropdown could go stale the moment their spending changed.

    The rule is created with **no payees**. Nothing is assigned to it here — the
    operator picks it on a row afterwards, through the same diff-then-write path
    as every other categorisation.
    """
    path = path or RULES_FILE
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    name = " ".join(str(name or "").split())

    if not name:
        raise ValueError("a category needs a name")
    if len(name) > 64:
        raise ValueError("a category name must be 64 characters or fewer")

    data = _load(path)
    rules = data.get("rules")
    if rules is None:
        raise ValueError(f"{path} has no `rules:` list to add to")

    existing = {str(spec.get("category")) for spec in rules if spec.get("category")}
    # Case-insensitively, because the ledger matches its category names exactly and
    # `Groceries` next to `groceries` is two categories and one confused chart.
    clash = next((c for c in existing if c.lower() == name.lower()), None)
    if clash:
        raise ValueError(f"{clash!r} already exists")

    append_to_seq(rules, {"title": name, "category": name, "payees": []})
    return ConfigChange(path=path, before=before, after=_dump(data))


def plan_remove_category(name: str, path: Path | None = None) -> ConfigChange:
    """Delete a category rule. **Refuses one that still has payees.** SPEC §32.

    The asymmetry was the bug: the UI could create a category and nothing could
    remove one, so a typo was permanent and `make e2e` silently accumulated a
    probe rule per run.

    Refusing a non-empty one is the whole safety of this. Deleting a rule that
    payees point at does not delete them — it strands them: `predict_category`
    stops matching, the rows quietly fall to uncategorised on the next push, and
    nothing anywhere says so. Reassign them first; then the rule is empty and
    this will take it.
    """
    path = path or RULES_FILE
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    name = " ".join(str(name or "").split())
    if not name:
        raise ValueError("which category?")

    data = _load(path)
    rules = data.get("rules")
    if rules is None:
        raise ValueError(f"{path} has no `rules:` list")

    index = next(
        (i for i, spec in enumerate(rules) if str(spec.get("category", "")) == name), None
    )
    if index is None:
        raise KeyError(f"no category named {name!r}")

    payees = rules[index].get("payees") or []
    if payees:
        raise ValueError(
            f"{name!r} still has {len(payees)} payee(s): {', '.join(map(str, payees[:4]))}"
            f"{'…' if len(payees) > 4 else ''}. Move them to another category first — "
            "removing the rule would strand them, and they would fall to "
            "uncategorised on the next push with nothing saying so."
        )

    # Guard the config the CHARTS read as well. A category named in `not_spend`
    # or in `large_oneoff.exclude_categories` that no longer exists excludes
    # nothing — silently, and §8's whole point is that a silent exclusion
    # failure reads as a plausible number.
    for key, holder in (
        ("not_spend", data.get("not_spend") or []),
        ("large_oneoff.exclude_categories", (data.get("large_oneoff") or {}).get("exclude_categories") or []),
    ):
        if name in [str(x) for x in holder]:
            raise ValueError(
                f"{name!r} is listed in `{key}`. Remove it there first, or the "
                "list would name a category that does not exist and exclude nothing."
            )

    del rules[index]
    normalise_comments(data)
    return ConfigChange(path=path, before=before, after=_dump(data))


def known_categories(path: Path | None = None) -> list[str]:
    """Categories that already have a rule. The UI offers only these."""
    data = _load(path or RULES_FILE)
    return sorted(
        {spec["category"] for spec in (data.get("rules") or []) if spec.get("category")}
    )


# --- the credit-card split. SPEC §103 ----------------------------------------


def plan_keep(
    external_id: str, amount: "Decimal | None", path: Path | None = None
) -> ConfigChange:
    """Set or clear how much of one settlement is THIS month's own spending.

    > "Credit Card I say put in last month but give option to split some amount
    >  if any in this month"

    `Attribution.keep` has existed since §73 and there was no way to fill it in
    — the operator would have had to open a YAML file and paste a namespaced
    `external_id` into it, which is exactly the terminal this app is meant to
    replace.

    **Keyed on the namespaced `external_id`**, never the bare `txn_id`: the bank
    sequences that per account and two accounts collide completely
    (non-negotiable 10). `None` or a non-positive amount removes the entry,
    because "nothing is kept" is the absence of a rule rather than a rule
    saying zero.

    Writes nothing itself. `plan_*` produces the new text, `apply` writes it,
    and the caller has shown a diff in between — the same two steps as every
    other editor here (§14).
    """
    path = path or ATTRIBUTION_FILE
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    data = _load(path)

    # **It does not switch the shift on.** SPEC §103.3. It used to, on the
    # reasoning that a `keep` written into a config with no `categories` records
    # an exception to a rule that does not exist. True, and the cure was worse:
    # typing 500 into one box moved three month buckets by tens of thousands,
    # because turning the policy on shifts *every* qualifying bill and not just
    # the one being edited. The operator's word for that was "broken", and they
    # were describing the surprise rather than a fault.
    #
    # Two decisions, two controls: `plan_settlement` owns the policy and this
    # owns the per-bill exception.
    _defaults(data)
    if "keep" not in data or data["keep"] is None:
        data["keep"] = {}
    keep = data["keep"]

    if amount is None or amount <= 0:
        keep.pop(external_id, None)
    else:
        # A plain string, not a float: this is money, and ruamel would write a
        # float back as one (non-negotiable 1). `load_attribution` parses it
        # with `Decimal` at the other end.
        keep[external_id] = str(amount)

    return ConfigChange(path=path, before=before, after=_dump(data))


def _defaults(data) -> None:
    """The two settings a shift needs, when the file is new. Never the policy."""
    data.setdefault("before_day", 10)
    data.setdefault("to_day", 22)
    if "categories" not in data or data["categories"] is None:
        data["categories"] = []


def plan_settlement(category: str, on: bool, path: Path | None = None) -> ConfigChange:
    """Whether a category's payments settle the **previous** month. SPEC §103.3.

    This is the policy, and it is deliberately its own control. It applies to
    every payment in that category paid on or before `before_day`, so switching
    it moves whole months at once — which is right, and which is exactly why it
    must not be a side effect of typing an amount into one bill's box.

    Off is the absence of the category, not a flag saying false: `Attribution`
    reads `categories` as the list of what shifts, and an empty list shifts
    nothing, which is the behaviour before this feature existed.
    """
    path = path or ATTRIBUTION_FILE
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    data = _load(path)
    _defaults(data)

    categories = data["categories"]
    present = category in list(categories)
    if on and not present:
        append_to_seq(categories, category)
    elif present and not on:
        for index, name in enumerate(list(categories)):
            if name == category:
                del categories[index]
                break

    return ConfigChange(path=path, before=before, after=_dump(data))


# --- what counts as earnings. SPEC §112 --------------------------------------


def plan_earnings(category: str, counts: bool, path: Path | None = None) -> ConfigChange:
    """Whether money arriving under `category` is **earned**. SPEC §112.

    > "Earning definition or any other definition is different for anyone so we
    >  cant generalize instead give an option i guess"

    They are right, and the shape of the existing rule is why it mattered.
    `not_earnings` is an **allow-list**: `earnings_only` names what counts, and
    everything else arriving is tagged `not-earnings`. That is safe against
    over-counting and it means a freshly registered account — whose payees
    nobody has classified — reports **₹0 earned** against real deposits.
    Measured on one: six deposits arrived and all six were excluded.

    An allow-list is the right mechanism and the wrong thing to bury in a YAML
    file, because the list *is* the definition and the definition is personal. A
    refund is not income for anyone; money from a parent is income for some
    people and a transfer for others. So it is editable from the page where
    payees are named, and nothing here decides it.

    Adding is `append_to_seq`, never `.append()` — §14: the comments in this
    file cost real money to acquire and a plain round-trip destroys them.
    """
    path = path or RULES_FILE
    before = path.read_text(encoding="utf-8") if path.exists() else ""
    data = _load(path)

    block = data.get("not_earnings")
    if block is None:
        # The rule itself is missing. Creating it with an EMPTY allow-list would
        # tag every deposit as not-earnings, which is the failure this function
        # exists to fix — so a new block starts by counting the thing being
        # asked about and nothing else.
        data["not_earnings"] = block = {
            "title": "Not earnings",
            "tag": "not-earnings",
            "earnings_only": [],
        }
    if block.get("earnings_only") is None:
        block["earnings_only"] = []
    listed = block["earnings_only"]

    present = category in list(listed)
    if counts and not present:
        append_to_seq(listed, category)
    elif present and not counts:
        for index, name in enumerate(list(listed)):
            if name == category:
                del listed[index]
                break

    return ConfigChange(path=path, before=before, after=_dump(data))


def earnings_categories(path: Path | None = None) -> list[str]:
    """The categories currently counted as earnings."""
    path = path or RULES_FILE
    if not path.exists():
        return []
    block = (_load(path) or {}).get("not_earnings") or {}
    return [str(c) for c in (block.get("earnings_only") or [])]

