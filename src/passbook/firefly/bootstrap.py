"""Create Firefly rules and bills from config/. SPEC §8.

Idempotent: matches on title and skips what already exists, so re-running is a
no-op rather than a pile of duplicates.

**Verified against the running instance (v6.6.6), not from memory:**

  * `app/Api/V1/Requests/Models/Rule/StoreRequest.php` — the request shape
    (`title`, `rule_group_title`, `trigger`, `triggers[]`, `actions[]`,
    `strict`, `stop_processing`, `active`), and that `rule_group_title` carries
    `belongsToUser:rule_groups,title`, i.e. the group must already exist.
  * `app/Support/Request/GetRuleConfiguration.php` — valid trigger types are
    `array_keys(config('search.operators'))`.
  * `app/Transformers/RuleTransformer.php:131` — a *prohibited* (negated)
    trigger has no column of its own; it is stored as a `-` prefix on
    `trigger_type`, and the API surfaces it as `prohibited: true`.
  * `config/firefly.php` — `set_category` and `add_tag` are both
    "context" actions, so each requires a value.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import yaml

from .client import FireflyClient, ValidationFailed

log = logging.getLogger(__name__)

RULES_FILE = Path("config/rules.yaml")
BILLS_FILE = Path("config/bills.yaml")


@dataclass
class BootstrapResult:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    existing: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed


def load_rules(path: Path | None = None) -> dict:
    path = path or RULES_FILE
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _payload(spec: dict, group_title: str) -> dict:
    """One category rule: any listed payee -> set category (+ optional tag)."""
    triggers = []
    for payee in spec.get("payees") or []:
        # description is pushed as "<display name> (<channel>)", so a
        # starts-with on the display name identifies the payee exactly.
        triggers.append({"type": "description_starts", "value": payee})
    if spec.get("notes_contains"):
        triggers.append({"type": "notes_contains", "value": spec["notes_contains"]})
    if spec.get("notes_starts"):
        triggers.append({"type": "notes_starts", "value": spec["notes_starts"]})

    actions = []
    if spec.get("category"):
        actions.append({"type": "set_category", "value": spec["category"]})
    if spec.get("tag"):
        actions.append({"type": "add_tag", "value": spec["tag"]})

    return {
        "title": spec["title"],
        "description": f"passbook: {spec.get('category') or spec.get('tag')}",
        "rule_group_title": group_title,
        "trigger": "store-journal",
        # ANY trigger matches — one rule can list several payees.
        "strict": False,
        "active": True,
        # Never short-circuit a later rule. SPEC §8.
        "stop_processing": False,
        "triggers": [{**t, "active": True, "stop_processing": False} for t in triggers],
        "actions": [{**a, "active": True, "stop_processing": False} for a in actions],
    }


def _not_earnings_payload(spec: dict, group_title: str) -> dict:
    """Tag every deposit that is not one of the true earnings sources.

    Inverted on purpose. A payee list would need maintaining every time a new
    counterparty appears, and would silently count an unlisted inflow as
    income; this defaults the other way. It also cannot touch a withdrawal,
    which is what makes the tag safe to filter on from the spend side too.
    """
    triggers = [{"type": "transaction_type", "value": "deposit"}]
    for payee in spec.get("earnings_only") or []:
        triggers.append({"type": "description_starts", "value": payee, "prohibited": True})

    return {
        "title": spec["title"],
        "description": f"passbook: {spec['tag']} (every deposit but the earnings sources)",
        "rule_group_title": group_title,
        "trigger": "store-journal",
        # ALL must hold: it is a deposit, and it is none of the earnings sources.
        "strict": True,
        "active": True,
        "stop_processing": False,
        "triggers": [{**t, "active": True, "stop_processing": False} for t in triggers],
        "actions": [
            {"type": "add_tag", "value": spec["tag"], "active": True, "stop_processing": False}
        ],
    }


def _large_oneoff_payload(spec: dict, group_title: str, threshold: Decimal) -> dict:
    """Strict rule: a big withdrawal that is not one of the excluded categories.

    `prohibited: true` is the negation; see the module docstring for how it is
    stored. Ordered last so the excluded categories are already assigned.
    """
    triggers = [
        {"type": "transaction_type", "value": "withdrawal"},
        {"type": "amount_more", "value": str(threshold)},
    ]
    # Exclusions on the incoming payload. These are the ones that actually
    # fire: description and notes are set before any rule runs.
    for payee in spec.get("exclude_payees") or []:
        triggers.append({"type": "description_starts", "value": payee, "prohibited": True})
    for fragment in spec.get("exclude_notes") or []:
        triggers.append({"type": "notes_contains", "value": fragment, "prohibited": True})
    # Inert at store time — the category is not committed yet when this rule
    # runs — but correct if the rule is re-run manually over stored data.
    for category in spec.get("exclude_categories") or []:
        triggers.append({"type": "category_is", "value": category, "prohibited": True})

    return {
        "title": spec["title"],
        "description": "passbook: unusually large spending",
        "rule_group_title": group_title,
        "trigger": "store-journal",
        # ALL triggers must hold: big, a withdrawal, and none of the exclusions.
        "strict": True,
        "active": True,
        "stop_processing": False,
        "triggers": [{**t, "active": True, "stop_processing": False} for t in triggers],
        "actions": [
            {"type": "add_tag", "value": spec["tag"], "active": True, "stop_processing": False}
        ],
    }


def bootstrap(
    client: FireflyClient, config: dict, threshold: Decimal, dry_run: bool = False
) -> BootstrapResult:
    result = BootstrapResult()
    group = config.get("rule_group") or {"title": "passbook"}
    group_title = group["title"]

    existing_groups = {g["attributes"]["title"] for g in client.rule_groups()}
    if group_title not in existing_groups:
        if not dry_run:
            client.store_rule_group(
                {"title": group_title, "description": group.get("description", ""), "active": True}
            )
        result.created.append(f"rule group {group_title!r}")
    else:
        result.existing.append(f"rule group {group_title!r}")

    payloads = [_payload(spec, group_title) for spec in config.get("rules") or []]

    if config.get("not_earnings"):
        payloads.append(_not_earnings_payload(config["not_earnings"], group_title))

    if config.get("large_oneoff"):
        payloads.append(_large_oneoff_payload(config["large_oneoff"], group_title, threshold))

    # Idempotent by title, but NOT skip-if-present: adding a payee to a rule in
    # rules.yaml has to reach Firefly, or the config and the engine drift apart
    # silently. That drift is not theoretical — three payees were added to
    # rules.yaml, bootstrap skipped the existing rules, and the next re-push
    # produced six uncategorised rows because Firefly's rules had never heard
    # of the new names.
    existing_rules = {r["attributes"]["title"]: r for r in client.rules()}
    for payload in payloads:
        title = payload["title"]
        current = existing_rules.get(title)

        if current is not None and not _rule_differs(current["attributes"], payload):
            result.existing.append(title)
            continue

        if dry_run:
            (result.updated if current else result.created).append(title)
            continue

        try:
            if current is not None:
                client.update_rule(current["id"], payload)
                result.updated.append(title)
            else:
                client.store_rule(payload)
                result.created.append(title)
        except ValidationFailed as exc:
            result.failed.append((title, f"{exc}: {exc.errors}"))
            log.warning("rule %r rejected: %s", title, exc.errors)
    return result


def _rule_differs(live: dict, wanted: dict) -> bool:
    """Compare only what we set. Firefly adds ids, timestamps and defaults."""

    def shape(triggers, actions, strict):
        return (
            bool(strict),
            sorted(
                (t["type"], str(t["value"]), bool(t.get("prohibited")))
                for t in triggers
            ),
            sorted((a["type"], str(a["value"])) for a in actions),
        )

    return shape(
        [t for t in live.get("triggers", []) if t["type"] != "user_action"],
        live.get("actions", []),
        live.get("strict"),
    ) != shape(wanted["triggers"], wanted["actions"], wanted["strict"])


def load_bills(path: Path | None = None) -> list[dict]:
    """SPEC §8: bills.yaml ships empty. On the reference statement nothing met
    §9's recurrence test (>=3 occurrences, median gap 25-35 days), so there is
    nothing to create and no placeholder is invented."""
    path = path or BILLS_FILE
    if not path.exists():
        return []
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded.get("bills") or []
