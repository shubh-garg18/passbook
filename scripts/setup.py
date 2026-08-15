#!/usr/bin/env python3
"""First-run wizard. SPEC §22.4.

    python3 scripts/setup.py          (or `make setup`, or double-click a launcher)

Takes someone from a fresh clone to a working ledger without a terminal
tutorial. Standard library only, and it must run before `uv sync` has ever
happened — so it cannot import `passbook`.

**The ordering problem it exists to solve.** `docker-compose.yml` declares
`FIREFLY_TOKEN: ${FIREFLY_TOKEN:?…}`, so compose refuses to start the `web`
service until a token exists. The token can only be created from inside a
running Firefly. A single `docker compose up` therefore cannot work on a fresh
install, and the first thing a new user would see is an interpolation error
about a variable they have never heard of.

So the stack comes up in two stages: database and Firefly first, then the token,
then the rest. `make up` does the same, for the same reason.

Everything here is re-runnable. It never overwrites an existing `.env`, and it
never overwrites a secret.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import re
import secrets
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"

BOLD, GREEN, RED, YELLOW, DIM, OFF = (
    "\033[1m", "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"
)
if os.name == "nt" and not os.environ.get("WT_SESSION"):
    BOLD = GREEN = RED = YELLOW = DIM = OFF = ""

ALPHANUM = string.ascii_letters + string.digits


def say(message: str = "") -> None:
    print(message)


def step(number: int, title: str) -> None:
    say(f"\n{BOLD}{number}. {title}{OFF}")


def die(message: str, *lines: str) -> None:
    say(f"\n{RED}{message}{OFF}")
    for line in lines:
        say(f"   {line}")
    say()
    sys.exit(1)


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"   {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        say()
        die("cancelled.")
    return answer or default


def compose(*args: str, check: bool = True, capture: bool = False):
    cmd = ["docker", "compose", *args]
    if capture:
        return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    return subprocess.run(cmd, cwd=ROOT, check=check)


# ── .env ─────────────────────────────────────────────────────────────────────
# Secrets are alphanumeric on purpose. The value passes through compose
# interpolation, a text substitution here, and `set -a; . ./.env` in three make
# targets; restricting the alphabet means never having to re-check it against
# any of them. SPEC §5.


def token(length: int) -> str:
    return "".join(secrets.choice(ALPHANUM) for _ in range(length))


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV.is_file():
        return values
    for line in ENV.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")
    return values


def set_env(key: str, value: str) -> None:
    """Replace in place, never append a duplicate.

    Quoted, because a value with a space in it (an asset account name, almost
    always) is a syntax error to `set -a; . ./.env`. SPEC §7.2.
    """
    quoted = f'"{value}"' if (" " in value or not value) else value
    lines = ENV.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = f"{key}={quoted}"
            break
    else:
        lines.append(f"{key}={quoted}")
    ENV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        ENV.chmod(0o600)
    except OSError:
        pass  # Windows filesystems do not carry the mode; harmless.


def create_env() -> None:
    if ENV.is_file():
        say(f"   {DIM}.env already exists — keeping it, and every secret in it.{OFF}")
        return
    if not EXAMPLE.is_file():
        die(".env.example is missing — this is not a complete checkout.")
    ENV.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        ENV.chmod(0o600)
    except OSError:
        pass
    set_env("APP_KEY", token(32))       # exactly 32; Firefly refuses anything else
    set_env("DB_PASSWORD", token(40))
    set_env("PASSBOOK_WEB_SECRET", token(48))
    say(f"   {GREEN}wrote .env{OFF} with a fresh APP_KEY, database password and session secret.")
    say(f"   {DIM}Never delete or change APP_KEY once Firefly has started: it{OFF}")
    say(f"   {DIM}encrypts the API keypair, and a new one invalidates your token.{OFF}")


# ── the stack ────────────────────────────────────────────────────────────────


def firefly_port() -> int:
    return int(read_env().get("FIREFLY_HOST_PORT") or 8080)


def http(url: str, tok: str | None = None, timeout: int = 10):
    request = urllib.request.Request(url)
    request.add_header("Accept", "application/json")
    if tok:
        request.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def wait_for_firefly(port: int, seconds: int = 420) -> bool:
    """Firefly runs ~60 database migrations on first boot. It is not hung."""
    say(f"   waiting for Firefly on port {port} — first boot runs the database")
    say(f"   {DIM}migrations and takes a minute or two{OFF}")
    deadline = time.time() + seconds
    dots = 0
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=5) as r:
                if r.status == 200:
                    say(f"\n   {GREEN}Firefly is up.{OFF}")
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        dots += 1
        print("." if dots % 5 else f". {int(deadline - time.time())}s left ", end="", flush=True)
        time.sleep(3)
    say()
    return False


# ── the token ────────────────────────────────────────────────────────────────


def looks_like_a_pat(value: str) -> str | None:
    """The shape check that catches the wrong credential before the API does.

    A Personal Access Token is an RS256 JWT: three dot-separated base64url
    segments, about a thousand characters. The **Command line token** on the
    Profile page is a different credential, short and dotless, and it cannot
    authenticate the API. Picking that one up cost an hour once; the message
    below is the whole point of this function.
    """
    if value.count(".") != 2:
        return (
            "that is not a Personal Access Token — it has no dots in it.\n"
            "   You have probably copied the 'Command line token' from the Profile\n"
            "   page. That is a different credential and will not work here.\n"
            "   Go to Options -> Remote access and tokens -> Personal Access Tokens."
        )
    if not value.startswith("eyJ"):
        return "that does not start with `eyJ`, so it is not a JWT. Copy the whole token."
    if len(value) < 200:
        return f"that is only {len(value)} characters; a PAT is around a thousand. Copy all of it."
    return None


def token_expiry(value: str) -> str | None:
    """Decode `exp` locally, for printing only. No signature check, no call."""
    try:
        payload = value.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(padded))
        from datetime import datetime, timezone

        return datetime.fromtimestamp(claims["exp"], timezone.utc).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 — this is decoration, never a gate
        return None


def validate_token(port: int, value: str) -> tuple[bool, str]:
    """Use it. A token that has not authenticated anything is a guess."""
    try:
        about = http(f"http://localhost:{port}/api/v1/about", value)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "Firefly rejected it (401). Wrong or expired token."
        return False, f"Firefly answered {exc.code}."
    except (urllib.error.URLError, OSError) as exc:
        return False, f"could not reach Firefly: {exc}"
    version = (about.get("data") or {}).get("version", "?")
    return True, f"authenticated against Firefly III {version}"


def asset_accounts(port: int, value: str) -> list[str]:
    try:
        payload = http(f"http://localhost:{port}/api/v1/accounts?type=asset", value)
    except Exception:  # noqa: BLE001
        return []
    return [a["attributes"]["name"] for a in payload.get("data", [])]


# ── steps ────────────────────────────────────────────────────────────────────


def main() -> int:
    say(f"\n{BOLD}passbook — first-run setup{OFF}")
    say(f"{DIM}Everything runs on this machine. Nothing is uploaded anywhere.{OFF}")

    step(1, "Checking what is installed")
    preflight = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "preflight.py")], cwd=ROOT
    )
    if preflight.returncode != 0:
        die("Setup cannot continue until the items above are installed.")

    step(2, "Writing configuration")
    create_env()

    step(3, "Starting the database and Firefly III")
    say(f"   {DIM}The web UI comes up in step 6 — it needs the token from step 5.{OFF}")
    if compose("up", "-d", "--wait", "db", "app", check=False).returncode != 0:
        die(
            "the database or Firefly did not start.",
            "Run `docker compose logs db app` to see why.",
            "If the error mentions a port already in use, set FIREFLY_HOST_PORT in",
            ".env to a free port and match APP_URL and FIREFLY_URL to it.",
        )
    port = firefly_port()
    if not wait_for_firefly(port):
        die(
            "Firefly did not answer in time.",
            f"Check `docker compose logs app`, then open http://localhost:{port}",
        )

    env = read_env()

    step(4, "Creating your Firefly account")
    url = f"http://localhost:{port}"
    if env.get("FIREFLY_TOKEN"):
        say(f"   {DIM}A token is already in .env — skipping registration.{OFF}")
    else:
        say(f"   Opening {BOLD}{url}{OFF} in your browser.")
        say("   The first account you register there becomes the admin, and")
        say("   registration then closes. Use any email; nothing is sent.")
        say()
        say(f"   Then set the currency: {BOLD}Options -> Preferences -> INR{OFF}")
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 — headless is fine, the URL is printed
            pass
        ask("Press Enter once you have registered and set the currency", " ")

    step(5, "Creating the API token")
    value = env.get("FIREFLY_TOKEN", "")
    if value:
        say(f"   {DIM}Using the FIREFLY_TOKEN already in .env.{OFF}")
    else:
        say(f"   In Firefly, go to {BOLD}Options -> Remote access and tokens{OFF}")
        say(f"   (that is {url}/profile/oauth), then:")
        say("     - under Personal Access Tokens, click Create New Token")
        say("     - name it `passbook`, leave everything else alone, Create")
        say("     - copy the whole token out of the box that appears")
        say()
        say(f"   {YELLOW}NOT the 'Command line token' on the Profile page.{OFF} That is a")
        say("   different credential, it will not authenticate the API, and it is")
        say("   the single most common way to lose an hour here.")
        say()
        try:
            webbrowser.open(f"{url}/profile/oauth")
        except Exception:  # noqa: BLE001
            pass

    while True:
        if not value:
            value = ask("Paste the token")
        problem = looks_like_a_pat(value)
        if problem:
            say(f"   {RED}{problem}{OFF}")
            value = ""
            continue
        good, detail = validate_token(port, value)
        if good:
            expires = token_expiry(value)
            say(f"   {GREEN}{detail}{OFF}")
            if expires:
                say(f"   {DIM}It expires on {expires}. `passbook doctor` warns 30 days out.{OFF}")
            break
        say(f"   {RED}{detail}{OFF}")
        value = ""

    set_env("FIREFLY_TOKEN", value)
    set_env("FIREFLY_URL", url)
    set_env("APP_URL", url)

    step(6, "Choosing the account to post into")
    names = asset_accounts(port, value)
    chosen = env.get("PASSBOOK_ASSET_ACCOUNT", "")
    if chosen:
        say(f"   {DIM}Already set to {chosen!r}.{OFF}")
    elif not names:
        say("   Firefly holds no asset account yet. Create one now:")
        say(f"     {url}/accounts/create/asset")
        say()
        say(f"   {YELLOW}Its opening balance must be your statement's OPENING balance,{OFF}")
        say("   dated on or before the first transaction — not your current")
        say("   balance. Firefly's wizard invites you to enter the current one,")
        say("   which double-counts and leaves the account negative.")
        ask("Press Enter once the account exists", " ")
        names = asset_accounts(port, value)

    if not chosen:
        if len(names) == 1:
            chosen = names[0]
            say(f"   Only one asset account: {GREEN}{chosen!r}{OFF}")
        elif names:
            say("   Which account do your statements belong to?")
            for index, name in enumerate(names, 1):
                say(f"     {index}. {name}")
            while True:
                pick = ask("Number")
                if pick.isdigit() and 1 <= int(pick) <= len(names):
                    chosen = names[int(pick) - 1]
                    break
                say(f"   {RED}Pick a number from the list.{OFF}")
        else:
            die("still no asset account in Firefly. Create one and re-run `make setup`.")
        set_env("PASSBOOK_ASSET_ACCOUNT", chosen)

    step(7, "Your bank account number")
    number = env.get("PASSBOOK_ACCOUNT_NUMBER", "")
    if number:
        say(f"   {DIM}Already set (ends {number[-4:]}).{OFF}")
    else:
        say("   This is the safety check that stops a statement from somebody")
        say("   else's account being imported into your ledger. It stays on this")
        say("   machine, is never logged in full, and never leaves .env.")
        while True:
            number = ask("Your full account number")
            if number.isdigit() and len(number) >= 8:
                break
            say(f"   {RED}Digits only, and at least 8 of them.{OFF}")
        set_env("PASSBOOK_ACCOUNT_NUMBER", number)

    step(8, "Starting the web UI")
    if compose("up", "-d", "--wait", check=False).returncode != 0:
        die(
            "the web UI did not start.",
            "Run `docker compose logs web` to see why.",
        )
    say(f"   {GREEN}everything is up.{OFF}")

    step(9, "Setting a password for the web UI")
    auth = ROOT / "config" / "web-auth.json"
    if auth.is_file():
        say(f"   {DIM}config/web-auth.json already exists — keeping it.{OFF}")
    else:
        say("   The web UI has its own password and a second factor, because")
        say("   Tailscale access is a planned feature and auth added afterwards")
        say("   is auth that never gets added.")
        say()
        result = subprocess.run(
            ["docker", "compose", "run", "--rm", "-i", "-T",
             "--entrypoint", "passbook", "web", "web-password"],
            cwd=ROOT,
        )
        if result.returncode != 0 or not auth.is_file():
            say(f"   {YELLOW}Skipped.{OFF} Run `make web-password` when you are ready;")
            say("   the UI will say 'Not set up yet' until you do.")

    say(f"\n{GREEN}{BOLD}Done.{OFF}\n")
    say(f"  passbook   {BOLD}http://localhost:8081{OFF}")
    say(f"  Firefly    {BOLD}{url}{OFF}")
    say()
    say("  Next: download a statement from your bank's net banking, then open")
    say("  the passbook page and drop the file on Upload.")
    say()
    say(f"  {DIM}Never upload a bank statement to an online converter. It carries{OFF}")
    say(f"  {DIM}your account number, your address and your counterparties' details.{OFF}")
    say()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\ncancelled.")
        sys.exit(130)
