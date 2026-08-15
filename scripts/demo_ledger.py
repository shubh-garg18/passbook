#!/usr/bin/env python3
"""Load the fixture into a throwaway Firefly, for screenshots. SPEC §22.1.

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
import json
import os
import secrets
import string
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

NET = "passbook_demo_net"
PG = "passbook_demo_db"
FF = "passbook_demo_app"
PORT = 8097
DB, USER = "firefly", "demo"

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
    for name in (FF, PG):
        run("docker", "rm", "-f", name, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run("docker", "network", "rm", NET, check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("demo stack removed")


def http(url: str, tok: str | None = None, payload: dict | None = None,
         method: str = "GET", timeout: int = 30):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data:
        request.add_header("Content-Type", "application/json")
    if tok:
        request.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode()
    return json.loads(body) if body else {}


def wait(url: str, seconds: int = 420) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            pass
        print(".", end="", flush=True)
        time.sleep(3)
    return False


def mint_token() -> str:
    """Register a user and mint a Personal Access Token, with no human in it.

    Registration goes through `POST /register`, the same form a real user fills
    in — Firefly's `system:create-first-user` artisan command refuses outside
    `APP_ENV=testing`, and flipping a demo container into testing mode to work
    around that would stop this exercising the path anyone else takes.

    The token then comes from `POST /oauth/personal-access-tokens`, which is the
    endpoint the Passport UI itself calls. There is no artisan command that
    mints a PAT, and forging one would mean signing a JWT with the instance's
    own Passport key.

    The token is returned and never printed: a maintainer runs this on their own
    machine, but a token in scrollback is a token in a screen recording.
    """
    import http.cookiejar
    import re

    email = "demo@example.com"
    password = token(24) + "aA1!"

    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    base = f"http://localhost:{PORT}"

    def form_token(path: str) -> str:
        page = opener.open(f"{base}{path}", timeout=30).read().decode()
        found = re.search(r'name="_token"\s+value="([^"]+)"', page)
        if not found:
            if path == "/register":
                # Firefly closes registration once the first account exists, so
                # this means the stack is not fresh. The password generated on
                # the previous run was random and was never stored, so there is
                # no way back in — rebuild rather than guess.
                raise SystemExit(
                    "this Firefly already has an account, so registration is closed "
                    "and the generated password is gone. Run "
                    "`python scripts/demo_ledger.py --down` and build again."
                )
            raise SystemExit(f"no _token field on {path}; Firefly's form changed shape")
        return found.group(1)

    def csrf() -> str:
        for cookie in jar:
            if cookie.name == "XSRF-TOKEN":
                return urllib.parse.unquote(cookie.value)
        raise SystemExit("no XSRF-TOKEN cookie; Firefly's session handling changed")

    body = urllib.parse.urlencode({
        "_token": form_token("/register"),
        "email": email,
        "password": password,
        "password_confirmation": password,
    }).encode()
    opener.open(urllib.request.Request(f"{base}/register", data=body), timeout=60).read()

    # Passport needs a "personal access client" to exist before it can issue a
    # PAT, and a fresh Firefly has none — the first token created through the UI
    # is what normally makes one. Without it, POST /oauth/personal-access-tokens
    # answers 500 with a stack trace and no useful message. Measured, not
    # assumed: the oauth_clients table was empty.
    subprocess.run(
        ["docker", "exec", FF, "php", "artisan", "passport:client", "--personal",
         "--name=passbook demo", "-n"],
        capture_output=True, text=True, check=True,
    )

    opener.open(f"{base}/profile", timeout=30).read()
    request = urllib.request.Request(
        f"{base}/oauth/personal-access-tokens",
        data=json.dumps({"name": "demo-screenshots", "scopes": []}).encode(),
        method="POST",
    )
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    request.add_header("X-Requested-With", "XMLHttpRequest")
    request.add_header("X-XSRF-TOKEN", csrf())
    payload = json.loads(opener.open(request, timeout=30).read().decode())
    if "accessToken" not in payload:
        raise SystemExit(f"no accessToken in the response: {payload}")
    return payload["accessToken"]


def build(shoot_tag: str | None = None) -> int:
    from passbook import service
    from passbook.config import Account

    if not FIXTURE.is_file():
        raise SystemExit(f"no fixture at {FIXTURE}")

    down()
    app_key = token(32)
    db_password = token(32)

    print("starting a scratch stack (its own network, nothing shared with live)")
    run("docker", "network", "create", NET, check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    run("docker", "run", "-d", "--name", PG, "--network", NET,
        "-e", f"POSTGRES_USER={USER}", "-e", f"POSTGRES_PASSWORD={db_password}",
        "-e", f"POSTGRES_DB={DB}", image(r"postgres:[^\s]+"),
        stdout=subprocess.DEVNULL)
    run("docker", "run", "-d", "--name", FF, "--network", NET,
        "-p", f"127.0.0.1:{PORT}:8080",
        "-e", f"APP_KEY={app_key}", "-e", "APP_ENV=production", "-e", "APP_DEBUG=false",
        "-e", f"APP_URL=http://localhost:{PORT}",
        "-e", "SITE_OWNER=demo@example.com", "-e", "DEFAULT_LANGUAGE=en_US",
        "-e", "TZ=Asia/Kolkata", "-e", "TRUSTED_PROXIES=**",
        "-e", "LOG_CHANNEL=stack", "-e", "APP_LOG_LEVEL=warning",
        "-e", "HEALTHCHECK_PATH=/health",
        "-e", "DB_CONNECTION=pgsql", "-e", f"DB_HOST={PG}", "-e", "DB_PORT=5432",
        "-e", f"DB_DATABASE={DB}", "-e", f"DB_USERNAME={USER}",
        "-e", f"DB_PASSWORD={db_password}",
        image(r"fireflyiii/core:[^\s]+"), stdout=subprocess.DEVNULL)

    print("waiting for Firefly (first boot runs its migrations) ", end="", flush=True)
    if not wait(f"http://localhost:{PORT}/health"):
        raise SystemExit("\nFirefly never became healthy")
    print(" up")

    tok = mint_token()
    print("  token minted (never printed, never written to .env)")

    parsed = service.parse_statement(FIXTURE)
    opening = parsed.meta.opening_balance
    first = min(t.txn_date for t in parsed.transactions)

    print(f"creating {ASSET_ACCOUNT!r} with the fixture's opening balance {opening}")
    http(f"http://localhost:{PORT}/api/v1/accounts", tok, method="POST", payload={
        "name": ASSET_ACCOUNT,
        "type": "asset",
        "account_role": "defaultAsset",
        "currency_code": "INR",
        # The trap §22.4 names: this must be the OPENING balance, dated on or
        # before the first transaction — not the current one.
        "opening_balance": str(opening),
        "opening_balance_date": first.isoformat(),
    })

    account = Account(slug=SLUG, bank="canara",
                      account_number=parsed.meta.account_number,
                      asset_account=ASSET_ACCOUNT)

    tokens = sorted({t.payee for t in parsed.transactions if t.payee})
    assigned = {name: DEMO_CATEGORIES[i % len(DEMO_CATEGORIES)]
                for i, name in enumerate(tokens)}
    # A couple of the largest get a not_spend category, so the charts show the
    # hatched excluded remainder the whole design turns on (§18.2).
    biggest = sorted(parsed.transactions,
                     key=lambda t: t.debit or 0, reverse=True)[:2]
    for index, txn in enumerate(biggest):
        if txn.payee:
            assigned[txn.payee] = NOT_SPEND[index % len(NOT_SPEND)]

    pushed = failed = 0
    for txn in parsed.transactions:
        debit = txn.debit is not None
        amount = txn.debit if debit else txn.credit
        payee = txn.payee or f"Unknown ({txn.channel})"
        payload = {
            "error_if_duplicate_hash": True,
            "apply_rules": False,
            "transactions": [{
                "type": "withdrawal" if debit else "deposit",
                "date": txn.txn_date.isoformat(),
                "amount": str(amount),
                "description": f"{payee} ({txn.channel})",
                "source_name": ASSET_ACCOUNT if debit else payee,
                "destination_name": payee if debit else ASSET_ACCOUNT,
                "currency_code": "INR",
                "notes": txn.narration,
                "external_id": account.external_id(txn.txn_id),
                "category_name": assigned.get(txn.payee or "", "Uncategorised"),
                "tags": ["reversal"] if txn.is_reversal else [],
            }],
        }
        try:
            http(f"http://localhost:{PORT}/api/v1/transactions", tok,
                 method="POST", payload=payload)
            pushed += 1
        except urllib.error.HTTPError as exc:
            failed += 1
            if failed < 3:
                print(f"  {txn.txn_id}: {exc.code} {exc.read().decode()[:160]}")

    print(f"\npushed {pushed}, failed {failed}")

    if shoot_tag:
        return shoot(shoot_tag, tok, parsed.meta.account_number)

    print()
    print("Screenshots, in one command (nothing to copy and paste):")
    print("    uv run python scripts/demo_ledger.py --shoot demo")
    print()
    print(f"Tear it down with:  python scripts/demo_ledger.py --down")
    return 0


def shoot(tag: str, tok: str, account_number: str) -> int:
    """Run `scripts/shoot.py` against the demo stack.

    Done here rather than printed as instructions because the token is the one
    thing that would otherwise have to be copied out of scrollback, and a token
    in scrollback is a token in a screen recording. The environment is passed to
    the child process and never written to `.env`.
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
        "FIREFLY_URL": f"http://localhost:{PORT}",
        "FIREFLY_TOKEN": tok,
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
        print(f"\nThe demo stack is still up on port {PORT}; --down removes it.")
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
