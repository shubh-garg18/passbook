#!/usr/bin/env python3
"""First-run wizard.

    python3 scripts/setup.py          (or `make setup`, or double-click a launcher)

Takes someone from a fresh clone to a working ledger without a terminal
tutorial. Standard library only, and it must run before `uv sync` has ever
happened — so it cannot import `passbook` until it has installed it.

**It used to be twice this long.** The ledger was a separate application, so
the wizard had to register an account inside it, create an API token, set a
currency, and bring the stack up in two stages because compose refused to start
anything until that token existed. Four of the six steps, and every one of them
a place to get stuck. They are gone: the ledger is passbook\'s own tables, so
there is nothing to log into and nothing to paste.

Everything here is re-runnable. It never overwrites an existing `.env`, and it
never overwrites a secret.
"""

from __future__ import annotations

import os
import secrets
import shutil
import string
import subprocess
import sys
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
    set_env("DB_PASSWORD", token(40))
    set_env("PASSBOOK_WEB_SECRET", token(48))
    say(f"   {GREEN}wrote .env{OFF} with a fresh database password and session secret.")
    say(f"   {DIM}Keep it. Changing DB_PASSWORD locks you out of your own ledger,{OFF}")
    say(f"   {DIM}and it is the one thing in here with no other copy.{OFF}")


# ── the stack ────────────────────────────────────────────────────────────────


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

#: What a bank statement looks like on disk. Anything else in Downloads is
#: somebody else's file and is never offered.
STATEMENT_SUFFIXES = {".xls", ".xlsx", ".csv", ".pdf", ".html", ".htm"}



def find_statement() -> Path | None:
    """A statement already sitting in inbox/, if there is one."""
    inbox = ROOT / "inbox"
    if not inbox.is_dir():
        return None
    files = [p for p in sorted(inbox.iterdir())
             if p.is_file() and not p.name.startswith(".")]
    return files[0] if files else None


def recent_downloads(limit: int = 5) -> list[Path]:
    """Plausible statements in the usual downloads folder, newest first.

    A statement arrives in Downloads and then has to be moved, which is a step
    that exists for no reason other than that nothing looked there. Nothing is
    copied without being chosen, and the list is filtered by suffix so this
    never offers somebody an unrelated document.
    """
    downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        return []
    try:
        files = [
            p for p in downloads.iterdir()
            if p.is_file()
            and not p.name.startswith(".")
            and p.suffix.lower() in STATEMENT_SUFFIXES
        ]
    except OSError:
        return []
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[:limit]


def offer_a_statement() -> Path | None:
    """Let the operator pick one rather than move a file by hand.

    Returns the statement now in `inbox/`, or None if they would rather place
    it themselves. **Copied, never moved** — the download stays where it was,
    because a setup step that relocates somebody's file is a setup step that
    loses it when they re-run.
    """
    candidates = recent_downloads()
    if not candidates:
        return None
    say(f"   {DIM}Found these in your Downloads folder:{OFF}")
    for index, path in enumerate(candidates, start=1):
        say(f"     {BOLD}{index}{OFF}  {path.name}")
    say(f"     {BOLD}0{OFF}  none of these — I will put one in inbox/ myself")
    while True:
        answer = ask("Which one is your bank statement?", "0")
        if answer in ("0", ""):
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(candidates):
            source = candidates[int(answer) - 1]
            inbox = ROOT / "inbox"
            inbox.mkdir(exist_ok=True)
            target = inbox / source.name
            shutil.copy2(source, target)
            say(f"   {GREEN}copied {source.name} into inbox/{OFF} "
                f"{DIM}(the original is untouched){OFF}")
            return target
        say(f"   {RED}Type a number from the list.{OFF}")


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


def create_asset_account(name: str, opening, on) -> bool:
    """The step the old setup got wrong, and now cannot.

    That wizard invited your CURRENT balance, which counts the closing figure
    twice and leaves the account negative. The statement's OPENING balance,
    dated on or before the first transaction, is the correct value — and it is
    in the file, so nobody needs to be told twice.

    Written through passbook's own store, in the project's virtualenv, because
    by this point `uv sync` has run.
    """
    snippet = (
        "import sys, datetime\n"
        "from decimal import Decimal\n"
        "from passbook.config import load_settings\n"
        "from passbook.store import open_ledger\n"
        "name, opening, on = sys.argv[1], Decimal(sys.argv[2]), "
        "datetime.date.fromisoformat(sys.argv[3])\n"
        "with open_ledger(load_settings()) as store:\n"
        "    store.store_account(name, opening, on, 'INR')\n"
    )
    done = subprocess.run(
        ["uv", "run", "python", "-c", snippet, name, str(opening), on.isoformat()],
        cwd=ROOT, capture_output=True, text=True,
    )
    if done.returncode == 0:
        return True
    say(f"   {YELLOW}could not create the account automatically.{OFF}")
    if done.stderr.strip():
        say(f"   {DIM}{done.stderr.strip().splitlines()[-1]}{OFF}")
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

    step(3, "Starting passbook")
    if compose("up", "-d", "--wait", check=False).returncode != 0:
        die(
            "the stack did not start.",
            "Run `docker compose logs` to see why.",
            "If the error mentions a port already in use, set PASSBOOK_DB_PORT in",
            ".env to a free port — nothing else needs to change.",
        )
    say(f"   {GREEN}everything is up.{OFF}")

    env = read_env()

    step(4, "Your bank account")
    chosen = env.get("PASSBOOK_ASSET_ACCOUNT", "")
    number = env.get("PASSBOOK_ACCOUNT_NUMBER", "")

    if chosen and number:
        say(f"   {DIM}Already set: {chosen!r}, ending {number[-4:]}.{OFF}")
    else:
        # Everything below is IN the statement — the account number, the
        # opening balance and the date it applies from. Reading it beats asking,
        # and it removes the one mistake a setup wizard usually invites.
        statement = find_statement()
        if statement:
            say(f"   Found {BOLD}{statement.name}{OFF} in inbox/.")
        else:
            statement = offer_a_statement()
        if statement is not None:
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
                chosen = ask("Name this account", "Bank savings")
                if create_asset_account(chosen, opening, first):
                    say(f"   {GREEN}created {chosen!r} with the statement's opening "
                        f"balance.{OFF}")
                    say(f"   {DIM}Not your CURRENT balance: that counts the closing{OFF}")
                    say(f"   {DIM}figure twice and leaves the account short forever.{OFF}")
                set_env("PASSBOOK_ASSET_ACCOUNT", chosen)
        else:
            # No statement, or one this build cannot read. Ask, as before —
            # and the first upload will create the account either way.
            say(f"   {YELLOW}Falling back to asking.{OFF}")
            if not number:
                while True:
                    number = ask("Your full bank account number")
                    if number.isdigit() and len(number) >= 8:
                        break
                    say(f"   {RED}Digits only, at least 8 of them.{OFF}")
                set_env("PASSBOOK_ACCOUNT_NUMBER", number)
            if not chosen:
                chosen = ask("Name this account", "Bank savings")
                set_env("PASSBOOK_ASSET_ACCOUNT", chosen)
                say(f"   {DIM}It will be created, with the right opening balance,{OFF}")
                say(f"   {DIM}by the first statement you upload.{OFF}")

    step(5, "A password for passbook itself")
    auth = ROOT / "config" / "web-auth.json"
    if auth.is_file():
        say(f"   {DIM}config/web-auth.json already exists — keeping it.{OFF}")
    else:
        say("   passbook has its own password and a second factor. Reaching it")
        say("   from another device is a planned feature, and auth added")
        say("   afterwards is auth that never gets added.")
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
