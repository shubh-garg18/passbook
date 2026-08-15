"""Edit config/*.yaml in place, preserving comments. SPEC §14.

`rules.yaml`'s comments carry knowledge that cost real money to acquire —
`NYXN XWUBQ  # a KFC franchise, not a person`, and the other three tokens D10
records as misread. Plain PyYAML round-trips destroy every one of them, so
this uses ruamel.yaml, which preserves comments, key order and formatting.

Nothing here writes without the caller having shown a diff first. `plan_*`
produces the new text, `apply` writes it — deliberately two steps.
"""

import copy
import difflib
import io
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML

from .config import PAYEE_ALIASES
from .firefly.bootstrap import RULES_FILE

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
    head, sep, tail = token.value.partition("\n")
    if not tail.strip():
        return  # an inline comment only; it belongs where it is
    token.value = head + sep
    moved = copy.copy(token)
    moved.value = tail
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
) -> ConfigChange:
    """Assign tokens to categories in rules.yaml.

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


def known_categories(path: Path | None = None) -> list[str]:
    """Categories that already have a rule. The UI offers only these."""
    data = _load(path or RULES_FILE)
    return sorted(
        {spec["category"] for spec in (data.get("rules") or []) if spec.get("category")}
    )
