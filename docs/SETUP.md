# Setting up passbook

One command, one question. About twenty minutes, most of it Docker
downloading, and 3 GB of disk. You need a computer — Windows, Mac or Linux.

---

## The easy way: Claude Code

If you have [Claude Code](https://claude.com/claude-code), open a terminal
wherever you want passbook to live and paste this:

```
Clone https://github.com/shubh-garg18/passbook.git and set it up on this
machine by following its SETUP.md. Ask me for anything only I can provide.
```

It installs what is missing, sets everything up, and stops once to ask you for
a statement. Skip to [First sign-in](#first-sign-in).

---

## Or do it yourself

First, get a statement from your bank's net banking and save it on this
computer. Canara, SBI and Union Bank are read out of the box.

If your bank locks the PDF with a password, keep the password — the app asks
for it when you upload.

> **Never put a statement through an online converter.** It carries your
> account number, your address and the people you paid.

Now pick your system.

<details open>
<summary><b>Windows</b></summary>

1. Install [Docker Desktop](https://docs.docker.com/desktop/install/windows-install/)
   and [Python](https://www.python.org/downloads/windows/). In the Python
   installer, tick **Add python.exe to PATH**.
2. Start Docker Desktop and wait for the whale icon to stop moving.
3. Download passbook: open **Command Prompt** and run

   ```
   git clone https://github.com/shubh-garg18/passbook.git
   ```

4. Open the new `passbook` folder in File Explorer, go into `launchers`, and
   **double-click `start-passbook.cmd`**.

That is it. A black window opens, sets everything up, and asks you for a
statement.

</details>

<details>
<summary><b>Mac</b></summary>

1. Install [Docker Desktop](https://docs.docker.com/desktop/install/mac-install/).
   Python is already on your Mac.
2. Start Docker Desktop and wait for the whale icon to stop moving.
3. Download passbook: open **Terminal** and run

   ```
   git clone https://github.com/shubh-garg18/passbook.git
   ```

4. Open the new `passbook` folder in Finder, go into `launchers`, then
   **right-click `start-passbook.command` → Open**, and Open again.

   Right-click the first time, not double-click: macOS blocks anything it did
   not download from the App Store, and this asks it nicely.

</details>

<details>
<summary><b>Linux</b></summary>

1. Install Docker and Python 3.11 or newer from your distribution.
2. Then:

   ```bash
   git clone https://github.com/shubh-garg18/passbook.git
   cd passbook
   make setup
   ```

   No `make`? `python3 scripts/setup.py` does the same thing.

</details>

### What it asks you

**One question: which statement to read.** It offers whatever it finds in your
Downloads folder, so usually you just press Enter. It copies the file — your
download stays where it is — and takes your account number, your opening
balance and the start date straight out of it.

Then it asks you to pick a password for passbook itself, so nobody else on this
computer can open it.

Everything else it does on its own, including every database password. You can
run it again any time; it never overwrites anything.

> **Clone it — do not download the ZIP.** Updates arrive through `git pull`, and
> a ZIP quietly opts you out of the thing that stops an update breaking your
> ledger.

---

## First sign-in

Open **http://localhost:8081** and sign in with the password you chose.

That is all it asks for. passbook is only reachable from this computer, so your
password is the lock. If you ever want a second factor as well — worth it if
you reach passbook from your phone — turn it on from **Account**.

---

## Add your other accounts

The statement you gave it is set up already. For each of your other accounts:

**Accounts → Add an account**, and give it a statement from that account. It
reads the number and opening balance the same way.

Each account keeps its own ledger. The tabs along the top switch between them,
and **Combine** shows any of them together.

---

## Every day after that

Everything lives in the `launchers` folder inside the `passbook` folder you
downloaded. Nothing here needs a terminal.

| To do this | Double-click this |
|---|---|
| **Start it** | `launchers/start-passbook` |
| **Stop it** | `launchers/stop-passbook` |
| **Update it** | `launchers/update-passbook` — the app tells you when there is one |

<sub>On Windows those files end in `.cmd`, on a Mac `.command`, on Linux `.sh`.</sub>

Everything else happens in the app at **http://localhost:8081**. Once a week:
download a statement, drop it on **Upload**, check what it found, press the
button. [The weekly cycle →](usage.md)

---

## When something goes wrong

**It will not start.** Make sure Docker is running — the whale icon — then
double-click the start launcher again. It says what is missing and what to do.

**I forgot my password.** Double-click the start launcher, and when it opens
say so — or run `make web-password` (`uv run passbook web-password` on Windows)
to set a new one.

**A statement was rejected.** The message says why. If it says no reader
understood the file, it is a bank passbook does not read yet — [open an
issue](https://github.com/shubh-garg18/passbook/issues) and say which bank.

**A page is blank.** Stop it and start it again with the launchers.

Still stuck? [Open an issue](https://github.com/shubh-garg18/passbook/issues)
and paste what it said. That is the most useful bug report there is.
