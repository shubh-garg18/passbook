#!/usr/bin/env python3
"""Load the fixture into a throwaway ledger, for screenshots.

    uv run python scripts/demo_ledger.py            # build it
    uv run python scripts/demo_ledger.py --down     # tear it down

`scripts/shoot.py` renders whatever ledger the stack is holding, so on the
author's machine it produces pictures of real payees and real balances — which
is why `docs/shots/` is gitignored. Screenshots that go **into** the repository
have to come from somewhere else, and this is that somewhere: a parallel stack
on its own docker network, holding nothing but `tests/fixtures/statement.xls`.

It never touches the live stack, the live database, `.env`, `config/`,
`inbox/` or `archive/`. Everything it makes is namespaced `passbook_demo_*` and
removed by `--down`.

The categories below are **invented for the demo**, not derived from the fixture
tokens — D10 forbids inferring meaning from a truncated token, and a demo that
broke the project's own rule would be a poor advertisement for it. They exist so
the charts have more than one bar; assignment is round-robin, and the file says
so on the page.
"""

from __future__ import annotations

import argparse
import os
import secrets
import string
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

NET = "passbook_demo_net"
PG = "passbook_demo_db"
PORT = 5497
DB, USER = "passbook", "demo"

FIXTURE = ROOT / "tests" / "fixtures" / "statement.xls"
ASSET_ACCOUNT = "Demo savings account"
SLUG = "canara-1111"

# Round-robin over the payee tokens. Not a claim about what any token means.
DEMO_CATEGORIES = [
    "Groceries", "Eating Out", "Transport", "Utilities", "Shopping",
    "Health", "Rent", "Subscriptions", "Gifts", "Cash",
]
NOT_SPEND = ["Investments", "Transfers"]


def token(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def run(*args: str, check: bool = True, **kwargs):
    return subprocess.run(args, check=check, **kwargs)


def image(pattern: str) -> str:
    compose = (ROOT / "docker-compose.yml").read_text()
    import re

    match = re.search(pattern, compose)
    if not match:
        raise SystemExit(f"could not find {pattern} in docker-compose.yml")
    return match.group(0)


def down() -> None:
    for name in (PG,):
        run("docker", "rm", "-f", name, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run("docker", "network", "rm", NET, check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("demo stack removed")


def build(shoot_tag: str | None = None) -> int:
    from passbook import service
    from passbook.config import Account
    from passbook.push import build_split
    from passbook.store import open_ledger

    if not FIXTURE.is_file():
        raise SystemExit(f"no fixture at {FIXTURE}")

    down()
    db_password = token(32)

    print("starting a scratch database (its own network, nothing shared with live)")
    run("docker", "network", "create", NET, check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run("docker", "run", "-d", "--name", PG, "--network", NET,
        "-p", f"127.0.0.1:{PORT}:5432",
        "-e", f"POSTGRES_USER={USER}", "-e", f"POSTGRES_PASSWORD={db_password}",
        "-e", f"POSTGRES_DB={DB}", image(r"postgres:[^\s]+"),
        stdout=subprocess.DEVNULL)

    dsn = f"postgresql://{USER}:{db_password}@127.0.0.1:{PORT}/{DB}"
    print("waiting for postgres ", end="", flush=True)
    store = None
    for _ in range(120):
        try:
            store = open_ledger(dsn=dsn)
            break
        except Exception:  # noqa: BLE001 — not ready yet is the normal answer
            print(".", end="", flush=True)
            time.sleep(1)
    if store is None:
        raise SystemExit("\nthe scratch database never became ready")
    print(" up")

    parsed = service.parse_statement(FIXTURE)
    opening = parsed.meta.opening_balance
    first = min(t.txn_date for t in parsed.transactions)

    print(f"creating {ASSET_ACCOUNT!r} with the fixture's opening balance {opening}")
    # The trap a setup wizard usually falls into: this must be the OPENING
    # balance, dated before the first transaction — not the current one.
    store.store_account(ASSET_ACCOUNT, opening, first, "INR")

    account = Account(slug=SLUG, bank="canara",
                      account_number=parsed.meta.account_number,
                      asset_account=ASSET_ACCOUNT)

    tokens = sorted({t.payee for t in parsed.transactions if t.payee})
    assigned = {name: DEMO_CATEGORIES[i % len(DEMO_CATEGORIES)]
                for i, name in enumerate(tokens)}
    # A couple of the largest get a not_spend category, so the charts show the
    # hatched excluded remainder the whole design turns on.
    biggest = sorted(parsed.transactions,
                     key=lambda t: t.debit or 0, reverse=True)[:2]
    for index, txn in enumerate(biggest):
        if txn.payee:
            assigned[txn.payee] = NOT_SPEND[index % len(NOT_SPEND)]

    pushed = failed = 0
    for txn in parsed.transactions:
        split = build_split(txn, account)
        split["category"] = assigned.get(txn.payee or "", "Uncategorised")
        try:
            store.store_transaction(split)
            pushed += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if failed < 3:
                print(f"  {txn.txn_id}: {exc}")
    store.close()

    print(f"\nwrote {pushed}, failed {failed}")

    if shoot_tag:
        return shoot(shoot_tag, dsn, parsed.meta.account_number)

    print()
    print("Screenshots, in one command (nothing to copy and paste):")
    print("    uv run python scripts/demo_ledger.py --shoot demo")
    print()
    print("Tear it down with:  python scripts/demo_ledger.py --down")
    return 0


def shoot(tag: str, dsn: str, account_number: str) -> int:
    """Run `scripts/shoot.py` against the demo ledger.

    Done here rather than printed as instructions because the connection string
    is the one thing that would otherwise have to be copied out of scrollback,
    and a password in scrollback is a password in a screen recording. The
    environment is passed to the child process and never written to `.env`.
    """
    print(f"\nscreenshotting into docs/shots/{tag}/")
    print("  (a typecheck is not a build — run `make web-build` first if the")
    print("   bundle is stale, or these are pictures of the last build)")
    # Run in a SCRATCH working directory, not in the repo. Two reasons, and the
    # second is the one that matters:
    #
    # 1. `shoot.py` reads `archive/` for the Day Rail's clock map and for the
    #    sync age. With no archive the rail renders empty and the pictures
    #    advertise a broken chart.
    # 2. On a working install `archive/` holds the operator's REAL statements,
    #    so shooting from the repo root would put real payees and real times
    #    into pictures destined for `docs/screenshots/`. Exactly the leak §22.1
    #    exists to prevent, one directory away.
    #
    # The scratch dir gets the fixture as its only archived statement, and
    # `docs/` is symlinked back so the shots still land in the repo — `out` is
    # resolved absolutely inside shoot.py for that reason.
    import shutil
    import tempfile

    scratch = Path(tempfile.mkdtemp(prefix="passbook-demo-shots-"))
    archive = scratch / "archive" / SLUG
    archive.mkdir(parents=True)
    shutil.copy2(FIXTURE, archive / FIXTURE.name)
    (scratch / "backups").mkdir()
    # Only the EXAMPLE config, renamed into place. Copying `config/` wholesale
    # would hand the demo the operator's own rules.yaml and payee_aliases.yaml
    # on a working install, and their category names would land in pictures
    # destined for a public repository. The examples ship no categories at all
    # (D10), which is exactly what a demo should show.
    (scratch / "config").mkdir()
    for example in sorted((ROOT / "config").glob("*.example.yaml")):
        shutil.copy2(example, scratch / "config" / example.name.replace(".example", ""))
    (scratch / "docs").symlink_to(ROOT / "docs")
    print(f"  working from {scratch} — the fixture is its only archived statement")

    env = dict(os.environ)
    env.update({
        "PASSBOOK_DATABASE_URL": dsn,
        "PASSBOOK_ASSET_ACCOUNT": ASSET_ACCOUNT,
        "PASSBOOK_ACCOUNT_NUMBER": account_number,
        "PASSBOOK_WEB_SECRET": token(48),
    })
    try:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "shoot.py"), tag],
            cwd=scratch, env=env,
        )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    if result.returncode != 0:
        print("\nshoot.py failed — its traceback is above, not here.")
        print("If it could not import playwright or pyotp, they are dev-only:")
        print("    uv run --with playwright --with pyotp \\")
        print("        python scripts/demo_ledger.py --shoot demo")
        print(f"\nThe demo database is still up on port {PORT}; --down removes it.")
        return result.returncode
    print()
    print(f"Now LOOK at docs/shots/{tag}/, then copy the ones you want into")
    print("docs/screenshots/. The point of a screenshot here is that somebody looked.")
    print()
    print("Tear the stack down with:  python scripts/demo_ledger.py --down")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--down", action="store_true", help="remove the demo stack")
    parser.add_argument("--shoot", metavar="TAG",
                        help="after loading, screenshot into docs/shots/TAG/")
    args = parser.parse_args()
    if args.down:
        down()
        return 0
    return build(args.shoot)


if __name__ == "__main__":
    sys.exit(main())
