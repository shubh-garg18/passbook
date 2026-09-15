# Setting up passbook

**One command, then three questions.** Everything else is automatic.

Budget twenty minutes — mostly Docker downloading — and about 3 GB of disk. You
need a PC: Windows, macOS or Linux. Not a phone.

| | |
|---|---|
| **What you do** | install Docker, download one statement from your bank, run one command |
| **What it does** | every secret, the database, the ledger store, the API token, the currency, and your account created with the statement's own opening balance |
| **What it asks** | a login to create, which statement to read, and a password for passbook |

---

## Option A — you have Claude Code

Open a terminal in the folder where you want passbook, and paste this:

```
Clone https://github.com/shubh-garg18/passbook.git and set it up on this
machine by following its SETUP.md. Ask me for anything only I can provide.
```

Claude Code will install what is missing, run the wizard, and stop to ask you
for the two things it cannot know: a bank statement to read, and a password for
passbook. Everything else it does itself.

If you already have the repository cloned, `cd` into it and say
**"set this up by following SETUP.md"**.

---

## Option B — do it yourself

### 1. Install Docker

**Or skip this and let the wizard do it.** `make setup` inspects your machine,
works out what Docker needs *here*, prints the exact commands and asks before
running any of them. It never installs anything silently.

If you would rather do it yourself:

| | |
|---|---|
| **Linux** | `curl -fsSL https://get.docker.com \| sh` then `sudo usermod -aG docker $USER`, and **log out and back in** |
| **Windows** | [Docker Desktop](https://docs.docker.com/desktop/install/windows-install/) — let it enable WSL2 and restart if it asks |
| **macOS** | [Docker Desktop](https://docs.docker.com/desktop/install/mac-install/) — pick the build matching your chip |

Then `docker run --rm hello-world` should work.

> **"Start the Docker service" is four different commands** — `systemctl` on a
> systemd Linux, `service` on WSL2 without it, opening the app on Docker
> Desktop, and none of them if you are simply not in the `docker` group. The
> wrong one fails in a way that reads like a broken install, which is why
> `make preflight` works out which is right for you rather than guessing.

<details>
<summary><b>Terminal only, no Desktop app?</b></summary>

**Linux** — the command above already is that. Nothing else needed.

**Windows** — WSL2 with Docker Engine inside the distro. In PowerShell as
Administrator: `wsl --install -d Ubuntu`, restart, then inside Ubuntu:

```bash
sudo tee /etc/wsl.conf >/dev/null <<'CONF'
[boot]
systemd=true
CONF
```

`wsl --shutdown` from PowerShell, reopen Ubuntu, then the Linux command above.
**Keep the repository under `~/`, never `/mnt/c/`** — Postgres cannot set file
permissions across that boundary and it is pathologically slow. `make check`
refuses to run from the wrong side.

**macOS** — `brew install colima docker docker-compose && colima start`.
*Untested by this project.* The stack is four ordinary containers with no
privileged access, so there is no reason it should fail — but "no reason it
should not" is not a measurement, and everything else here is measured. If you
try it, [say whether it worked](https://github.com/shubh-garg18/passbook/issues).
</details>

### 2. Install Python 3.11+

Linux and macOS almost certainly have it (`python3 --version`). On Windows,
[python.org](https://www.python.org/downloads/windows/) — **tick "Add python.exe
to PATH"**.

Only the setup wizard uses it. passbook itself runs in Docker.

### 3. Download a statement

From your bank's net banking. Canara, SBI and Union Bank read out of the box;
for anything else, *Account menu → Add a bank* takes the column mapping once you
are signed in.

**Never upload it to an online converter.** It carries your account number,
customer ID, address and your counterparties' details.

### 4. Clone and run the wizard

```bash
git clone https://github.com/shubh-garg18/passbook.git
cd passbook
```

Then start it, whichever suits you:

| | Terminal | Double-click |
|---|---|---|
| **Linux / macOS** | `make setup` | `launchers/start-passbook.sh` · `.command` |
| **Windows** | `python scripts\setup.py` | `launchers\start-passbook.cmd` |

> **Windows has no `make`**, so `make setup` will not work there — use either
> column above instead. On macOS the first double-click is refused by Gatekeeper
> because nothing here is signed: right-click → **Open**, then Open again.

> **Clone it, do not download the ZIP.** `git pull` is how you get updates, and
> `make upgrade` reads the repository to know which migrations this install has
> already applied. A ZIP silently opts you out of the one mechanism that stops a
> pulled change breaking your ledger.

It will offer you any statement it finds in your **Downloads** folder, so you
usually do not have to move a file at all. It copies the one you pick — your
download stays where it is.

The wizard asks **two things** and does the rest:

| It asks | Note |
|---|---|
| Which statement to read | It takes your account number, opening balance and start date from the file. |
| A password for passbook | Plus a second factor on first sign-in. |

Everything else is automatic: secrets, the stack, and your account created with
the statement's **opening** balance.

There is no second application to install, no account to register, and no API
token to paste. There used to be all three.

It is safe to re-run. It never overwrites a secret.

### 5. Sign in

Open **http://localhost:8081**. First sign-in walks you through the second
factor: scan the QR, type one code back, and write down the **eight backup
codes** you are shown once. They are stored as salted digests — nobody,
including this app, can print them again.

Done. From here everything happens in the browser: [the weekly
cycle](docs/usage.md).

---

## If something goes wrong

```bash
make preflight     # what is missing, and where to get it
make check         # is the configuration sane
docker compose logs --tail=50
```

| Symptom | Cause |
|---|---|
| Docker daemon not reachable | Docker Desktop is not started, or on Linux you are not in the `docker` group yet — that needs a fresh login. |
| `make up` hangs on `Waiting` | The database is initialising on first boot. It is not stuck. |
| A port will not bind, but nothing is listening | On Windows/WSL a **Windows** process holds it and the distro cannot see it. `netstat -ano \| findstr :8081` names it. For the database, set `PASSBOOK_DB_PORT` in `.env`; nothing else has to change. |
| Postgres will not start | On WSL2, the repo is on `/mnt/c/`. Move it under `~/`. |

**If the wizard could not read your statement**, it falls back to asking for
your account number and a name for the account, and the first upload creates
the account for you — with the statement's **opening** balance, dated the day
before the first transaction. That detail matters: an account opened at your
*current* balance counts the closing figure twice and is short forever.

More in [operations.md](docs/operations.md).

## Managing your sign-in later

```bash
make web-password                # reset the password; KEEPS your second factor
make web-totp                    # enrolled? how many backup codes left?
make web-totp RESET=yes          # phone gone and codes used up
make web-totp FORGET_DEVICES=yes # revoke every remembered browser
```

There is **no password reset and no email**, deliberately — both mean storing
another credential for a single-operator tool on one machine.

---

## Every command, on Windows

`make` is a Unix tool and **Windows does not ship it**, so every `make …` in this
documentation needs a different command there. Each is a one-liner, and each does
exactly what the `make` target does — the target is only a shortcut for it:

| Documentation says | On Windows, run |
|---|---|
| `make setup` | `python scripts\setup.py` |
| `make preflight` | `python scripts\preflight.py` |
| `make up` | `docker compose up -d --wait` |
| `make down` | `docker compose down` |
| `make logs` | `docker compose logs -f` |
| `make sync` | `uv run passbook sync` |
| `make test` | `uv run pytest -q` |
| `make web-password` | `uv run passbook web-password` |
| `make web-totp` | `uv run passbook web-totp` |
| `make web-totp RESET=yes` | `uv run passbook web-totp --reset` |
| `make web-totp FORGET_DEVICES=yes` | `uv run passbook web-totp --forget-devices` |
| `make upgrade` | `uv run passbook upgrade` |
| `make verify-ledger` | `uv run passbook verify-ledger` |

**`make check`, `make backup`, `make verify-backup` and `make dr-drill` have no
native-Windows equivalent.** They are shell scripts that source `.env` and drive
`pg_dump`, and rewriting them for `cmd` would be a second implementation of the
one path that must never have two.

### The simpler answer, if you want every command to just work

Run passbook **inside WSL2**, which Docker Desktop already installs on your
machine. Open the Ubuntu terminal, and every command in this documentation works
exactly as written, backups included:

```bash
sudo apt install make          # the only thing missing
git clone https://github.com/shubh-garg18/passbook.git ~/passbook
cd ~/passbook && make setup
```

Keep the folder under `~/`, never `/mnt/c/` — Postgres cannot set file
permissions across that boundary. `make check` refuses to run from the wrong
side.
