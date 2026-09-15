"""An in-memory ledger, for tests and for reasoning about the real one.

This exists so the rule that tests never touch the network can survive a ledger
that lives in Postgres. It is not a stub: it enforces the same invariants the
schema does, because a double that accepts what the database would refuse is a
double that makes tests pass and production fail.

Specifically, and each of these is a constraint in `schema.sql`:

* `external_id` is unique. A second insert of the same identity raises rather
  than appending — the thing that went wrong when a content hash was the guard.
* `amount` is a `Decimal` and never negative. Direction lives in `kind`.
* `kind` is `withdrawal` or `deposit`, and nothing else. There is no third
  value: an opening balance is a column on the account, not a row that could
  turn up in a listing with no payee and no amount.
* A transaction must belong to an account that exists.
"""

from __future__ import annotations

from decimal import Decimal

from . import LedgerError


class MemoryLedger:
    """A ledger in a dict. Same invariants, no I/O."""

    def __init__(self) -> None:
        self._accounts: dict[str, dict] = {}
        self._rows: dict[str, dict] = {}

    # --- accounts ------------------------------------------------------------

    def asset_accounts(self) -> list[dict]:
        out = []
        for account in self._accounts.values():
            live = Decimal(account["opening_balance"])
            for row in self._rows.values():
                if row["account"] != account["name"]:
                    continue
                live += row["amount"] if row["kind"] == "deposit" else -row["amount"]
            out.append({**account, "current_balance": live})
        return out

    def store_account(self, name: str, opening, on, currency: str) -> None:
        if name in self._accounts:
            return
        self._accounts[name] = {
            "name": name,
            "opening_balance": Decimal(str(opening)),
            "opening_on": on,
            "currency": currency,
        }

    # --- transactions --------------------------------------------------------

    def account_transactions(self, account: str) -> list[dict]:
        rows = [dict(r) for r in self._rows.values() if r["account"] == account]
        rows.sort(key=lambda r: (r["txn_date"], r["external_id"]))
        return rows

    def identities(self, account: str) -> set[str]:
        return {r["external_id"] for r in self._rows.values() if r["account"] == account}

    def store_transaction(self, split: dict) -> None:
        external_id = str(split.get("external_id") or "")
        if not external_id:
            # The previous store allowed a row with no id at all, and those are
            # exactly the rows no check could ever reconcile.
            raise LedgerError("a transaction with no external_id cannot be stored")
        if external_id in self._rows:
            raise LedgerError(f"{external_id} is already in the ledger")
        account = str(split.get("account") or "")
        if account not in self._accounts:
            raise LedgerError(f"no asset account named {account!r}")

        kind = str(split.get("kind") or "")
        if kind not in ("withdrawal", "deposit"):
            raise LedgerError(f"{kind!r} is not a direction; use withdrawal or deposit")

        amount = Decimal(str(split["amount"]))
        if amount < 0:
            raise LedgerError("amount is positive; direction belongs in `kind`")

        self._rows[external_id] = {
            "external_id": external_id,
            "account": account,
            "kind": kind,
            "txn_date": split["txn_date"],
            "amount": amount,
            "description": split.get("description", ""),
            "counterparty": split.get("counterparty", ""),
            "category": split.get("category", ""),
            "notes": split.get("notes", ""),
            "txn_time": split.get("txn_time"),
            "currency": split.get("currency", ""),
            "tags": sorted(set(split.get("tags") or [])),
        }

    def update_transaction(self, external_id: str, fields: dict) -> None:
        """Sparse: only the keys given are touched.

        The fields config owns are description, category, counterparty and the
        managed tags. `amount`, `txn_date`, `kind`, `external_id` and `notes`
        are refused outright rather than merely left alone — an update that
        *could* move money is one that eventually does.
        """
        row = self._rows.get(external_id)
        if row is None:
            raise LedgerError(f"{external_id} is not in the ledger")
        forbidden = {"amount", "txn_date", "kind", "external_id", "notes", "account"}
        overreach = forbidden & set(fields)
        if overreach:
            raise LedgerError(
                f"an update may not touch {sorted(overreach)} — those come from "
                "the statement, and a rename that can move money is a rename "
                "that eventually does"
            )
        for key, value in fields.items():
            row[key] = sorted(set(value)) if key == "tags" else value

    def delete_transaction(self, external_id: str) -> None:
        self._rows.pop(external_id, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        self.close()
        return False

    def close(self) -> None:
        return None
