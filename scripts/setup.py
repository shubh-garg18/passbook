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
import shutil
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dockercheck  # noqa: E402  — a sibling script, not a package

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


def http(url: str, tok: str | None = None, payload: dict | None = None,
         method: str = "GET", timeout: int = 20):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if tok:
        request.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read().decode()
    return json.loads(body) if body.strip() else {}


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



# Run inside the project's virtualenv, which is where the parser lives. Kept as
# a string rather than a module so this file stays importable by a bare system
# python with nothing installed — which is what runs it on a fresh clone.
PARSE_SNIPPET = """
import sys
from passbook.loaders import load
meta, transactions = load(sys.argv[1])
first = min(t.txn_date for t in transactions)
print(f"{meta.account_number}|{meta.opening_balance}|{first.isoformat()}")
"""


# ── automatic provisioning ───────────────────────────────────────────────────
# Steps a person used to do by hand: register, find the token page, create a
# token, paste it, set the currency, create an asset account with the right
# opening balance. All of it is machine-doable, and every one of those steps was
# somewhere to go wrong — the "Command line token" mix-up and Firefly's
# invitation to enter your CURRENT balance are the two that cost real time.
#
# **What is pinned and what is not.** The currency and the account come from the
# REST API this project pins and reads from source. Registration and token
# minting do not: `POST /register` is a browser form and
# `POST /oauth/personal-access-tokens` is Passport's own endpoint. They are more
# likely to shift between Firefly versions than `/api/v1/*`.
#
# So this is best-effort by construction. Anything unexpected returns None and
# the wizard falls back to asking, which is exactly what it did before. A
# Firefly change degrades this to the old flow rather than to a dead end.


def form_field(opener, url: str, name: str = "_token") -> str | None:
    try:
        page = opener.open(url, timeout=30).read().decode()
    except Exception:  # noqa: BLE001
        return None
    found = re.search(rf'name="{name}"\s+value="([^"]+)"', page)
    return found.group(1) if found else None


# Firefly's own registration rule, MEASURED against the pinned tag rather than
# guessed: a 12-character password is rejected with "The password must be at
# least 16 characters." The form answers 200 and simply re-renders, so a wizard
# that only checked the status code would report success and then fail to log
# in. This is why the prompt says 16 and why the error below is surfaced.
MIN_FIREFLY_PASSWORD = 16


def _form_error(html: str) -> str | None:
    """Firefly renders validation failures into an alert-danger block."""
    found = re.search(r"alert-danger[^>]*>(.*?)</div>", html, re.S)
    if not found:
        return None
    text = re.sub(r"<[^>]+>", " ", found.group(1))
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def auto_provision(port: int, email: str, password: str) -> tuple[str | None, str | None]:
    """(token, error). Either may be None; both None means "not this build".

    An `error` is Firefly telling us the input was wrong — the caller should ask
    again. Both None means registration or Passport did not behave the way this
    build expects, and the caller should fall back to the manual path.
    """
    import http.cookiejar

    base = f"http://localhost:{port}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    token = form_field(opener, f"{base}/register")
    if token is None:
        return None, None  # registration closed, or the form changed shape
    try:
        body = urllib.parse.urlencode({
            "_token": token, "email": email,
            "password": password, "password_confirmation": password,
        }).encode()
        page = opener.open(
            urllib.request.Request(f"{base}/register", data=body), timeout=90
        ).read().decode()
    except Exception:  # noqa: BLE001
        return None, None

    # A rejected registration answers 200 and re-renders the form. The status
    # code is not the signal; being logged in afterwards is.
    if not any(c.name == "firefly_iii_session" for c in jar) or "/register" in page[:4000]:
        problem = _form_error(page)
        if problem:
            return None, problem

    # Passport needs a "personal access client" before it can issue a PAT, and a
    # fresh Firefly has none — the first token created through the UI is what
    # normally makes one. Without it the POST below answers 500 with a stack
    # trace and no useful message. Measured on a fresh 6.6.6: oauth_clients was
    # empty. Failure here is not fatal; one may already exist.
    subprocess.run(
        ["docker", "compose", "exec", "-T", "app", "php", "artisan",
         "passport:client", "--personal", "--name=passbook", "-n"],
        cwd=ROOT, capture_output=True, text=True,
    )

    try:
        opener.open(f"{base}/profile", timeout=30).read()
        csrf = next(
            (urllib.parse.unquote(c.value) for c in jar if c.name == "XSRF-TOKEN"), None
        )
        if csrf is None:
            return None, None
        request = urllib.request.Request(
            f"{base}/oauth/personal-access-tokens",
            data=json.dumps({"name": "passbook", "scopes": []}).encode(),
            method="POST",
        )
        for header, value in (
            ("Content-Type", "application/json"), ("Accept", "application/json"),
            ("X-Requested-With", "XMLHttpRequest"), ("X-XSRF-TOKEN", csrf),
        ):
            request.add_header(header, value)
        payload = json.loads(opener.open(request, timeout=30).read().decode())
    except Exception:  # noqa: BLE001
        return None, None
    return payload.get("accessToken"), None


def set_currency(port: int, tok: str, code: str = "INR") -> bool:
    """Enable the currency and make it primary. Pinned REST API, both of them:
    `POST /currencies/{code}/enable` and `/primary` exist on the pinned tag."""
    base = f"http://localhost:{port}/api/v1/currencies/{code}"
    for suffix in ("enable", "primary"):
        try:
            http(f"{base}/{suffix}", tok, payload={}, method="POST")
        except urllib.error.HTTPError as exc:
            if exc.code not in (200, 204, 422):  # 422 = already in that state
                return False
        except Exception:  # noqa: BLE001
            return False
    return True


def find_statement() -> Path | None:
    """A statement already sitting in inbox/, if there is one."""
    inbox = ROOT / "inbox"
    if not inbox.is_dir():
        return None
    files = [p for p in sorted(inbox.iterdir())
             if p.is_file() and not p.name.startswith(".")]
    return files[0] if files else None


def install_dependencies() -> bool:
    """Install the Python dependencies, so the wizard can read a statement.

    Reading the statement is what removes the two worst steps — typing an
    account number and getting the opening balance right — and it needs the
    parser, which needs the dependencies. On a fresh clone nothing is installed
    yet, so without this the wizard silently drops to the "ask them" path on the
    machine where it matters most: the new one.

    `uv` is the project's own tool and is by far the fastest; a plain venv is the
    fallback for a machine that has only Python. Either failing is not fatal.
    """
    if shutil.which("uv"):
        done = subprocess.run(
            ["uv", "sync", "--quiet"], cwd=ROOT, capture_output=True, text=True
        )
        if done.returncode == 0:
            sys.path.insert(0, str(ROOT / ".venv" / "lib"))
            return True
    venv = ROOT / ".venv"
    if not venv.exists():
        subprocess.run([sys.executable, "-m", "venv", str(venv)], capture_output=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        return False
    done = subprocess.run(
        [str(python), "-m", "pip", "install", "-q", "-e", str(ROOT)],
        capture_output=True, text=True,
    )
    return done.returncode == 0


def read_statement(path: Path):
    """(account_number, opening_balance, first_date) — or None if unreadable.

    Runs in whichever interpreter has the dependencies. This process is very
    likely the system python with nothing installed, so the parse is delegated to
    the project's own virtualenv when there is one — and the wizard falls back to
    asking if there is not.
    """
    venv_python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv_python.exists() and Path(sys.executable).resolve() != venv_python.resolve():
        probe = subprocess.run(
            [str(venv_python), "-c", PARSE_SNIPPET, str(path)],
            cwd=ROOT, capture_output=True, text=True,
        )
        if probe.returncode != 0:
            if probe.stderr.strip():
                say(f"   {YELLOW}could not read {path.name}{OFF}")
            return None
        try:
            import datetime

            number, opening, first = probe.stdout.strip().split("|")
            return number, opening, datetime.date.fromisoformat(first)
        except ValueError:
            return None
    try:
        sys.path.insert(0, str(ROOT / "src"))
        from passbook.loaders import load
    except Exception:  # noqa: BLE001
        return None
    try:
        meta, transactions = load(path)
    except Exception as exc:  # noqa: BLE001
        say(f"   {YELLOW}could not read {path.name}: {exc}{OFF}")
        return None
    if not transactions:
        return None
    return meta.account_number, meta.opening_balance, min(t.txn_date for t in transactions)


def create_asset_account(port: int, tok: str, name: str, opening, on) -> bool:
    """The step Firefly's own wizard gets wrong.

    Its setup invites your CURRENT balance, which counts the closing figure
    twice and leaves the account negative. The statement's OPENING balance,
    dated on or before the first transaction, is the correct value — and it is
    in the file, so nobody needs to be told twice.
    """
    try:
        http(f"http://localhost:{port}/api/v1/accounts", tok, method="POST", payload={
            "name": name, "type": "asset", "account_role": "defaultAsset",
            "currency_code": "INR",
            "opening_balance": str(opening),
            "opening_balance_date": on.isoformat(),
        })
        return True
    except Exception as exc:  # noqa: BLE001
        say(f"   {YELLOW}could not create the account automatically: {exc}{OFF}")
        return False


def fix_docker() -> bool:
    """Offer to run the fix for THIS machine. True if Docker works afterwards.

    Never silently: every command is printed and confirmed before it runs, and
    anything needing `sudo` says so. Installing system packages on someone's
    machine without asking is not a convenience, and a wizard that did it would
    deserve the distrust.
    """
    diagnosis = dockercheck.diagnose()
    if diagnosis.ok:
        return True

    say(f"   {YELLOW}{diagnosis.problem}.{OFF}")
    runnable = [f for f in diagnosis.fixes if f.command]
    manual = [f for f in diagnosis.fixes if not f.command]

    for fix in manual:
        say(f"   • {fix.label}")
        if fix.url:
            say(f"     {BOLD}{fix.url}{OFF}")
        for part in fix.note.split("\n"):
            if part:
                say(f"     {DIM}{part}{OFF}")

    if not runnable:
        return False

    say()
    say("   This machine needs:")
    for fix in runnable:
        say(f"     {BOLD}{' '.join(fix.command)}{OFF}")
        if fix.note:
            say(f"       {DIM}{fix.note}{OFF}")
    say()
    if ask("Run these now? [y/N]", "n").lower() not in ("y", "yes"):
        say(f"   {DIM}Fine — run them yourself and start me again.{OFF}")
        return False

    relogin = False
    for fix in runnable:
        say(f"\n   {DIM}$ {' '.join(fix.command)}{OFF}")
        if subprocess.run(fix.command, cwd=ROOT).returncode != 0:
            say(f"   {RED}that failed.{OFF} Run it yourself and start me again.")
            return False
        relogin = relogin or fix.then_relogin

    if relogin:
        # usermod does not touch the current session's groups. Saying "done"
        # here and then failing on the next docker call is the kind of quiet
        # wrongness this project keeps writing down.
        say()
        say(f"   {YELLOW}Log out and back in, then run `make setup` again.{OFF}")
        say(f"   {DIM}Group membership only applies to new sessions — nothing{OFF}")
        say(f"   {DIM}below would work until you do.{OFF}")
        return False

    return dockercheck.diagnose().ok


# ── steps ────────────────────────────────────────────────────────────────────


def main() -> int:
    say(f"\n{BOLD}passbook — first-run setup{OFF}")
    say(f"{DIM}Everything runs on this machine. Nothing is uploaded anywhere.{OFF}")

    step(1, "Checking what is installed")
    preflight = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "preflight.py")], cwd=ROOT
    )
    if preflight.returncode != 0:
        # Docker is the one thing this can often fix in place, and the one thing
        # most likely to be wrong. Anything else, the report above already named.
        if not dockercheck.diagnose().ok:
            say()
            if not fix_docker():
                die("Setup cannot continue until Docker is working.")
            say(f"\n   {GREEN}Docker is working now.{OFF}")
            if subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "preflight.py")], cwd=ROOT
            ).returncode != 0:
                die("Setup cannot continue until the items above are installed.")
        else:
            die("Setup cannot continue until the items above are installed.")

    step(2, "Writing configuration")
    create_env()

    step(3, "Starting the database and Firefly III")
    say(f"   {DIM}The web UI comes up later — it cannot start without a token.{OFF}")
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

    step(4, "Your Firefly account")
    url = f"http://localhost:{port}"
    value = env.get("FIREFLY_TOKEN", "")

    if value:
        say(f"   {DIM}A token is already in .env — skipping.{OFF}")
    else:
        say("   Firefly needs one login. The first account registered becomes the")
        say("   admin and registration then closes. Nothing is emailed anywhere,")
        say(f"   but you will need these to open Firefly itself at {url}.")
        say()
        email = ask("Email", "you@example.com")
        value = None
        while value is None:
            password = ask(f"Password ({MIN_FIREFLY_PASSWORD}+ characters)")
            if len(password) < MIN_FIREFLY_PASSWORD:
                # Firefly's own rule, and it rejects quietly — the form answers
                # 200 and re-renders, so catching it here is the difference
                # between a clear message and a mystery.
                say(f"   {RED}Firefly requires at least {MIN_FIREFLY_PASSWORD} "
                    f"characters. A short phrase works well.{OFF}")
                continue
            say(f"\n   {DIM}registering and creating an API token…{OFF}")
            value, problem = auto_provision(port, email, password)
            if value is None and problem:
                say(f"   {RED}Firefly rejected that: {problem}{OFF}")
                continue
            break

        if value:
            say(f"   {GREEN}done — no token to copy and paste.{OFF}")
            if set_currency(port, value):
                say(f"   {GREEN}currency set to INR.{OFF}")
        else:
            # Registration or Passport did not answer the way this build
            # expects — a Firefly version change, or an account that already
            # exists. Fall back to the manual path rather than to a dead end.
            say(f"   {YELLOW}could not do that automatically on this Firefly build.{OFF}")
            say("   Doing it by hand takes a minute:")
            say(f"     1. open {BOLD}{url}{OFF} and register (or sign in)")
            say(f"     2. {BOLD}Options -> Preferences{OFF} -> set the currency to INR")
            say(f"     3. {BOLD}Options -> Remote access and tokens{OFF}")
            say("        -> Personal Access Tokens -> Create New Token")
            say()
            say(f"   {YELLOW}NOT the 'Command line token' on the Profile page.{OFF} Different")
            say("   credential, will not authenticate the API, and it is the single")
            say("   most common way to lose an hour here.")
            say()
            try:
                webbrowser.open(f"{url}/profile/oauth")
            except Exception:  # noqa: BLE001
                pass

    # Whatever produced it, the token is used before it is trusted. A credential
    # that has not authenticated anything is a guess.
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
                say(f"   {DIM}Expires {expires}. `passbook doctor` warns 30 days out.{OFF}")
            break
        say(f"   {RED}{detail}{OFF}")
        value = ""

    set_env("FIREFLY_TOKEN", value)
    set_env("FIREFLY_URL", url)
    set_env("APP_URL", url)

    step(5, "Your bank account")
    chosen = env.get("PASSBOOK_ASSET_ACCOUNT", "")
    number = env.get("PASSBOOK_ACCOUNT_NUMBER", "")
    names = asset_accounts(port, value)

    if chosen and number:
        say(f"   {DIM}Already set: {chosen!r}, ending {number[-4:]}.{OFF}")
    else:
        # Everything below is IN the statement — the account number, the opening
        # balance and the date it applies from. Reading it beats asking, and it
        # removes the one mistake Firefly's own setup actively invites.
        statement = find_statement()
        if statement:
            say(f"   Found {BOLD}{statement.name}{OFF} in inbox/.")
            say(f"   {DIM}installing the parser so it can be read…{OFF}")
            install_dependencies()
        else:
            say("   Download one statement from your bank's net banking and put it")
            say(f"   in the {BOLD}inbox{OFF} folder here:")
            say(f"     {ROOT / 'inbox'}")
            say()
            say(f"   {DIM}Never upload it to an online converter — it carries your{OFF}")
            say(f"   {DIM}account number, address and counterparties' details.{OFF}")
            ask("Press Enter once it is there", " ")
            statement = find_statement()
            if statement:
                say(f"   {DIM}installing the parser so it can be read…{OFF}")
                install_dependencies()

        details = read_statement(statement) if statement else None
        if details:
            number, opening, first = details
            say(f"   Read it: account ending {BOLD}{number[-4:]}{OFF}, "
                f"opening balance {BOLD}{opening}{OFF} on {first}.")
            set_env("PASSBOOK_ACCOUNT_NUMBER", number)
            if not chosen:
                chosen = ask("Name this account in Firefly", "Bank savings")
                if chosen in names:
                    say(f"   {DIM}That account already exists — using it.{OFF}")
                elif create_asset_account(port, value, chosen, opening, first):
                    say(f"   {GREEN}created {chosen!r} with the statement's opening balance.{OFF}")
                    say(f"   {DIM}Not your CURRENT balance — Firefly's own wizard asks for{OFF}")
                    say(f"   {DIM}that, which double-counts and leaves the account negative.{OFF}")
                set_env("PASSBOOK_ASSET_ACCOUNT", chosen)
        else:
            # No statement, or one this build cannot read. Ask, as before.
            say(f"   {YELLOW}Falling back to asking.{OFF}")
            if not number:
                while True:
                    number = ask("Your full bank account number")
                    if number.isdigit() and len(number) >= 8:
                        break
                    say(f"   {RED}Digits only, at least 8 of them.{OFF}")
                set_env("PASSBOOK_ACCOUNT_NUMBER", number)
            if not chosen:
                names = asset_accounts(port, value)
                if len(names) == 1:
                    chosen = names[0]
                    say(f"   Only one asset account: {GREEN}{chosen!r}{OFF}")
                elif names:
                    say("   Which Firefly account do your statements belong to?")
                    for index, name in enumerate(names, 1):
                        say(f"     {index}. {name}")
                    while True:
                        pick = ask("Number")
                        if pick.isdigit() and 1 <= int(pick) <= len(names):
                            chosen = names[int(pick) - 1]
                            break
                        say(f"   {RED}Pick a number from the list.{OFF}")
                else:
                    say(f"   Create an asset account: {url}/accounts/create/asset")
                    say(f"   {YELLOW}Its opening balance must be your statement's OPENING{OFF}")
                    say("   balance, dated on or before the first transaction.")
                    ask("Press Enter once it exists", " ")
                    names = asset_accounts(port, value)
                    if not names:
                        die("still no asset account in Firefly. Create one and re-run `make setup`.")
                    chosen = names[0]
                set_env("PASSBOOK_ASSET_ACCOUNT", chosen)

    step(6, "Starting the web UI")
    if compose("up", "-d", "--wait", check=False).returncode != 0:
        die(
            "the web UI did not start.",
            "Run `docker compose logs web` to see why.",
        )
    say(f"   {GREEN}everything is up.{OFF}")

    step(7, "A password for passbook itself")
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
