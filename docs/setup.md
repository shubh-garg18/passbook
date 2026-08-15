# Setting up

You need **a PC** — Windows, macOS or Linux. This does not run on a phone or a
tablet, and it is not something you sign into; it runs on your own machine and
nothing leaves it.

Total time: about twenty minutes, most of it Docker downloading.

---

## 1. Install Docker Desktop

One installer, three platforms, and it manages everything underneath — including
WSL2 on Windows, which you do not have to set up yourself.

| | |
|---|---|
| Windows | https://docs.docker.com/desktop/install/windows-install/ |
| macOS | https://docs.docker.com/desktop/install/mac-install/ |
| Linux | https://docs.docker.com/desktop/install/linux-install/ |

Start it after installing and wait for the whale icon to stop animating. Nothing
below works until it has.

> **Windows:** the installer will offer to enable WSL2 and may ask to restart.
> Let it. That is the part people usually get wrong by hand.

> **A note for developers.** The author's own machine runs Docker Engine inside
> WSL2 Ubuntu, not Docker Desktop — that is SPEC D8, and §22.4 records why it is
> documented rather than recommended. It needs systemd enabled in
> `/etc/wsl.conf`, a `wsl --shutdown`, a package install inside the distro, and
> a group change with a re-login. Four manual steps, each of which fails quietly
> in its own way. Docker Desktop is one installer. If you already run Docker
> Engine, everything here works unchanged.

## 2. Install Python 3.11 or newer

Only the setup wizard needs it; passbook itself runs inside Docker.

- **Windows** — https://www.python.org/downloads/windows/ — tick **"Add
  python.exe to PATH"** in the installer.
- **macOS** — https://www.python.org/downloads/ or `xcode-select --install`.
- **Linux** — you already have it.

## 3. Get passbook

```bash
git clone https://github.com/shubh-garg18/passbook.git
cd passbook
```

Or download the ZIP from the repository page and unzip it.

> **On Windows with WSL2:** if you work inside the Linux distro, keep the folder
> under `~/`, never under `/mnt/c/`. Postgres cannot set file permissions across
> that boundary and it is pathologically slow. `make check` refuses to run from
> the wrong side. Using Docker Desktop from Windows itself, this does not arise.

## 4. Run the setup

**Double-click the launcher for your system:**

| | |
|---|---|
| Windows | `start-passbook.cmd` |
| macOS | `start-passbook.command` — first time, right-click → **Open**, because macOS blocks unsigned downloads |
| Linux | `./start-passbook.sh` |

Or, in a terminal:

```bash
make setup            # or: python3 scripts/setup.py
```

To see what is missing without changing anything:

```bash
make preflight        # or: python3 scripts/preflight.py
```

### What the wizard does

1. **Checks what is installed** and names anything missing, with a download link.
2. **Writes `.env`** with a fresh encryption key, database password and session
   secret. It refuses to overwrite an existing one, so it is safe to re-run.
3. **Starts Postgres and Firefly III.** First boot runs about sixty database
   migrations and takes a minute or two. It is not hung.
4. **Opens Firefly in your browser** so you can register. The first account you
   create becomes the admin and registration then closes. Any email works;
   nothing is sent anywhere.
5. **Walks you to the API token** and validates it before continuing.
6. **Asks which Firefly account** your statements post into.
7. **Asks for your bank account number** — the safety check that stops somebody
   else's statement importing into your ledger.
8. **Starts the web UI** at http://localhost:8081.
9. **Sets a password** for it, and walks you through the second factor.

It is re-runnable. If you stop halfway, run it again; it keeps everything it
already has.

---

## The two things that go wrong

### The token

In Firefly: **Options → Remote access and tokens** → Personal Access Tokens →
Create New Token.

**Not the "Command line token" on the Profile page.** That is a different
credential, it cannot authenticate the API, and it is the single most common way
to lose an hour here. Earlier versions of this project's own documentation sent
people to the wrong page.

A Personal Access Token is a JWT: roughly a thousand characters, starts `eyJ`,
two dots in it. Anything short and dotless is the wrong one. The wizard checks
the shape *and* then uses the token against the API — a token that has not
authenticated anything is a guess.

It expires **365 days** after issue, and Firefly warns you about nothing; expiry
just surfaces as a generic 401. `passbook doctor` decodes the expiry date
locally and warns 30 days out.

### The opening balance

When you create the asset account in Firefly, its opening balance must be your
statement's **opening** balance, dated on or before the first transaction.

Firefly's setup wizard invites you to enter your **current** balance instead.
Doing that counts the closing figure twice and leaves the account negative. If
that has already happened, edit the account and fix the opening balance — no
transactions need to be touched.

---

## Ports

Firefly is on **8080**, the passbook UI on **8081**, both bound to `127.0.0.1`
only — not reachable from your network.

If 8080 is taken, set `FIREFLY_HOST_PORT` in `.env` to a free port and change
`APP_URL` and `FIREFLY_URL` to match it. All three must agree.

> **A Windows-specific trap.** With WSL2 in `networkingMode=mirrored` the distro
> shares Windows's port space, so a **Windows** process holding 8080 makes the
> bind fail while `ss -ltnp` inside the distro shows nothing listening at all.
> `netstat.exe -ano | grep :8080` names it. On the machine this was measured on
> it was a background service — move passbook's port rather than killing it.

## Afterwards

```bash
make up       # start (also after a reboot)
make down     # stop; your data stays
make check    # verify the configuration
```

Then read the main [README](../README.md) for the weekly cycle, and
[upgrading.md](upgrading.md) before your first `git pull`.
