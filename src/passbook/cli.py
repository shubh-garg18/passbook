"""Typer entrypoint. SPEC §7.3.

`parse` and `payees` are read-only and make no network calls. `doctor`, `push`,
`sync`, `bootstrap` and `purge` talk to the ledger; the last three write.
"""

import json
import logging
import sys
import shutil
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import narration as narration_mod
from . import ops, service
from .config import (
    Account,
    RegistryError,
    default_slug,
    load_accounts,
    save_accounts,
    load_payee_aliases,
    load_settings,
)
from .rules import load_rules
from .store import LedgerError, open_ledger
from .purge import find_candidates
from .purge import purge as purge_transactions
from .push import build_split, push_transactions
from .loaders import load as load_statement
from . import reminders
from .loaders import read_grid
from .loaders._table import ParseError
from .models import Transaction, normalised
from .validate import (
    AccountMismatch,
    BalanceBreak,
    IntegrityError,
    UnknownAccount,
    assert_account,
    check,
)

app = typer.Typer(add_completion=False, help="Canara Bank -> the ledger ingest pipeline.")

TOKEN_WARN_DAYS = 30
console = Console()
err = Console(stderr=True)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )


def sync_staleness(target: Console | None = None) -> int | None:
    """Print how long since the last successful sync. Returns the age in days.

    One definition, called from `doctor`, `sync` and `make up`. The wording
    escalates because the two situations differ: past SYNC_STALE_DAYS you are
    late, past SYNC_URGENT_DAYS the oldest missing rows may already be outside
    the range Canara will still serve — and nothing here can recover those.
    Backups protect what reached the ledger; they cannot recover what never did.

    Reads `archive/`, not `inbox/`: a file lands there only after a *successful*
    push, so it records what actually reached the ledger rather than what was
    merely downloaded.
    """
    from .service import sync_status

    out = target or console
    st = sync_status()
    label = {
        "never": "[yellow]warn[/yellow] ",
        "ok": "[green]ok[/green]   ",
        "warn": "[yellow]warn[/yellow] ",
        "stale": "[red bold]STALE[/red bold]",
    }[st.state]
    out.print(f"  {label} {st.headline}")
    if st.detail:
        out.print(f"        [{'red' if st.state == 'stale' else 'dim'}]{st.detail}[/]")
    return st.age


def _read(path: Path) -> tuple:
    """Load, enrich with narration fields, validate. Exits non-zero on failure."""
    try:
        meta, transactions = load_statement(path)
    except ParseError as exc:
        err.print(f"[red]parse failed:[/red] {exc}")
        raise typer.Exit(2)

    narration_mod.enrich(transactions, load_payee_aliases())

    try:
        warnings = check(meta, transactions)
    except (BalanceBreak, IntegrityError) as exc:
        err.print(f"[red]validation failed:[/red] {exc}")
        raise typer.Exit(3)

    return meta, transactions, warnings


def _check_account(meta) -> str:
    """SPEC §6.7, §21.2. Returns a status line; exits non-zero on a real mismatch.

    **Asks the registry first.** §6.7's question changed when accounts became
    plural: it is no longer "is this MY account?" but "WHICH of my accounts is
    this?". Checking only `PASSBOOK_ACCOUNT_NUMBER` refused every statement
    belonging to the second account — measured, exit 4 on a file the upload page
    accepts happily, which is the two front ends disagreeing about the same file.

    The refusal itself is unchanged: an account in neither the registry nor
    `.env` still stops the command.
    """
    settings = load_settings()
    for account in load_accounts(settings=settings):
        if account.account_number.strip() == meta.account_number.strip():
            return f"[green]passes[/green] (routes to {account.slug}, {meta.masked_account})"

    configured = settings.passbook_account_number
    try:
        assert_account(meta, configured)
    except AccountMismatch as exc:
        if not configured:
            # Read-only command: warn rather than refuse. The refusal that
            # matters is on push, which is Phase 3.
            return f"[yellow]unverified[/yellow] ({exc})"
        err.print(f"[red]account assertion failed:[/red] {exc}")
        err.print(
            "[dim]If this is a second account of yours, register it first: "
            "`passbook accounts add <statement>`, or Account -> Add an account.[/dim]"
        )
        raise typer.Exit(4)
    return f"[green]passes[/green] (matches {meta.masked_account})"


@app.command()
def parse(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    as_json: bool = typer.Option(False, "--json", help="emit normalised JSON instead of a table"),
    verbose: bool = typer.Option(False, "-v", "--verbose", help="log the selected loader"),
) -> None:
    """Parse and validate a statement. Prints a table. Writes nothing."""
    _setup_logging(verbose)
    meta, transactions, warnings = _read(file)
    account_status = _check_account(meta)

    if as_json:
        console.print_json(json.dumps(normalised(meta, transactions, warnings)))
        return

    table = Table(show_lines=False, header_style="bold")
    for col in ("Date", "Txn ID", "Chan", "Payee", "Debit", "Credit", "Balance"):
        table.add_column(col, justify="right" if col in ("Debit", "Credit", "Balance") else "left")
    for txn in transactions:
        table.add_row(
            txn.txn_date.isoformat(),
            txn.txn_id,
            ("[cyan]REV[/cyan]" if txn.is_reversal else txn.channel),
            (txn.payee or "-")[:18],
            f"{txn.debit:,}" if txn.debit else "",
            f"{txn.credit:,}" if txn.credit else "",
            f"{txn.balance:,}",
        )
    console.print(table)

    debits = sum(t.debit for t in transactions if t.debit)
    credits = sum(t.credit for t in transactions if t.credit)
    console.print(
        f"\naccount        {meta.masked_account}   {meta.period_from} to {meta.period_to}\n"
        f"rows parsed    {len(transactions)}\n"
        f"continuity     [green]0 breaks[/green]  "
        f"({meta.opening_balance:,} -> {meta.closing_balance:,}, "
        f"final matches Closing Balance sentinel)\n"
        f"totals         withdrawn {debits:,}   deposited {credits:,}\n"
        f"account check  {account_status}\n"
        f"warnings       {len(warnings)}"
    )
    for warning in warnings:
        console.print(f"  [yellow]warn[/yellow] {warning}")


@app.command()
def inspect(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    rows: int = typer.Option(18, "--rows", "-n", help="how many rows to show"),
    password: str = typer.Option(
        None, "--password", prompt=False, help="for an encrypted PDF; never stored"
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Show a statement's raw grid, so you can write a bank profile. SPEC §27.

    **This is the tool that means you never have to send anyone your
    statement.** It prints what a parser sees — every cell as `repr()`, so a
    space is visibly a space — and then says which of the six required columns
    it could already match. Copy the header text it shows into
    `config/banks/<yourbank>.yaml` and passbook can read your bank. No Python.

    It parses nothing and pushes nothing. Safe on a file from any bank.

    Read the output for these, in order:

      1. Which row is the header, and its EXACT text (typos included).
      2. How an empty amount cell prints — `' '` is not `''`, and confusing the
         two is the likeliest silent bug in this whole project.
      3. The date format.
      4. Whether amounts carry thousands separators.
    """
    _setup_logging(verbose)
    from .loaders import sniff
    from .loaders._table import COL_ALIASES, REQUIRED_COLS, _all_aliases, norm
    from .loaders.profiles import known_banks

    kind = sniff(file)
    console.print(f"[bold]{file.name}[/bold] — container: [cyan]{kind}[/cyan]")
    profiles = known_banks()
    console.print(
        "profiles loaded: " + (", ".join(profiles) if profiles else "[dim]none[/dim]")
    )

    grid = _grid(file, kind, password)
    if grid is None:
        err.print(f"[red]no grid reader for {kind!r}[/red] — see docs/ADDING-A-BANK.md")
        raise typer.Exit(2)

    console.print(f"grid: {len(grid)} rows x {max((len(r) for r in grid), default=0)} cols\n")

    aliases = _all_aliases()
    best: tuple[int, dict[str, int]] | None = None
    for index, row in enumerate(grid[: min(len(grid), 50)]):
        found = {}
        for c, cell in enumerate(row):
            field = aliases.get(norm(cell))
            if field and field not in found:
                found[field] = c
        if len(found) > len(best[1] if best else {}):
            best = (index, found)

    table = Table(show_lines=False, header_style="bold", title="raw cells, as repr()")
    table.add_column("row", justify="right")
    width = max((len(r) for r in grid), default=0)
    for c in range(min(width, 9)):
        table.add_column(str(c), overflow="fold")
    for index, row in enumerate(grid[:rows]):
        marker = f"[green]{index}[/green]" if best and index == best[0] else str(index)
        cells = [repr(cell)[:34] for cell in row[:9]]
        cells += [""] * (min(width, 9) - len(cells))
        table.add_row(marker, *cells)
    console.print(table)

    console.print()
    if best and best[1]:
        console.print(f"best header guess: [green]row {best[0]}[/green]")
        for field, column in sorted(best[1].items(), key=lambda kv: kv[1]):
            console.print(f"  [green]ok[/green]   col {column:<2} -> {field}")
        missing = sorted(REQUIRED_COLS - best[1].keys())
        if missing:
            console.print(f"\n  [yellow]missing[/yellow]: {', '.join(missing)}")
            console.print(
                "\nWrite these into [bold]config/banks/<yourbank>.yaml[/bold], using the "
                "header text exactly as printed above:\n"
            )
            console.print("[dim]bank: yourbank\ncolumns:[/dim]")
            for field in missing:
                console.print(f'[dim]  "<the header cell for {field}>": {field}[/dim]')
            console.print(
                "\nMatching ignores case, spaces and punctuation, so you do not have to "
                "reproduce those exactly."
            )
        else:
            console.print("\n[green]every required column already matches[/green] — "
                          "`passbook parse` should work on this file.")
    else:
        console.print(
            "[yellow]no header row recognised.[/yellow] Find the row above whose cells are "
            "column names, and map each one in config/banks/<yourbank>.yaml. Required "
            f"fields: {', '.join(sorted(REQUIRED_COLS))}"
        )

    console.print(
        f"\n[dim]built-in aliases: {len(COL_ALIASES)}; "
        f"after profiles: {len(aliases)}[/dim]"
    )
    console.print("[dim]Full walkthrough: docs/ADDING-A-BANK.md[/dim]")


def _grid(file: Path, kind: str, password: str | None = None):
    """`read_grid`, with a terminal prompt for an encrypted PDF.

    The prompt is the only thing this adds. Everything else lives in
    `read_grid`, which the Add-a-bank page calls too and which must raise rather
    than prompt — see §34.
    """
    if kind == "pdf":
        from .loaders.pdf import PdfPasswordRequired, PdfPasswordWrong

        while True:
            try:
                return read_grid(file, kind, password)
            except (PdfPasswordRequired, PdfPasswordWrong) as exc:
                # Prompting belongs HERE and nowhere else. `read_grid` is called
                # by the web page too, which needs the exception so it can put a
                # password field on screen — a reader that prompts or exits is a
                # reader only a terminal can use.
                if not sys.stdin.isatty():
                    err.print(f"[red]{exc}[/red] Pass --password.")
                    raise typer.Exit(2) from exc
                console.print(f"[yellow]{exc}[/yellow]")
                password = typer.prompt("PDF password", hide_input=True)
    return read_grid(file, kind, password)


@app.command()
def payees(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    top: int = typer.Option(25, "--top", "-n", help="how many to show"),
    markdown: bool = typer.Option(
        False, "--markdown", help="emit a markdown table instead of a box table"
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Rank observed payee tokens by count and value.

    This is what categorisation rules get written from. SPEC D10: Canara
    truncates UPI payee names to ~10 characters, so a rule matching a full
    merchant name never fires. Write rules against the tokens printed here.
    """
    _setup_logging(verbose)
    _meta, transactions, _warnings = _read(file)

    # Grouped on the RAW token, not the alias: two tokens can share one display
    # name (a vendor with two QR codes), and the raw token is what rules match.
    groups: dict[tuple[str, str], list[Transaction]] = defaultdict(list)
    for txn in transactions:
        groups[(txn.payee or "(unparsed)", txn.channel)].append(txn)

    rows = []
    for (payee, channel), txns in groups.items():
        # Not named `out` — that is the --out parameter, and shadowing it here
        # made the overwrite guard fail with an AttributeError instead of firing.
        withdrawn = sum((t.debit for t in txns if t.debit), Decimal(0))
        deposited = sum((t.credit for t in txns if t.credit), Decimal(0))
        dates = sorted(t.txn_date for t in txns)
        alias = next((t.payee_alias for t in txns if t.payee_alias), "") or ""
        rows.append((payee, alias, channel, len(txns), withdrawn, deposited, dates[0], dates[-1]))
    rows.sort(key=lambda r: (r[4] + r[5], r[3]), reverse=True)

    headers = ("#", "Token", "Len", "Alias", "Chan", "Txns",
               "Withdrawn", "Deposited", "Total", "First", "Last")

    def cells(i, r):
        payee, alias, channel, count, out, inc, first, last = r
        return (
            str(i), payee, str(len(payee)) if payee != "(unparsed)" else "-",
            alias, channel, str(count),
            f"{out:,}" if out else "", f"{inc:,}" if inc else "", f"{out + inc:,}",
            first.isoformat(), last.isoformat(),
        )

    note = (
        "Tokens are truncated by the bank to ~10 chars, so rules must match these "
        "exact strings, not full merchant names (SPEC D10). Grouped on the raw "
        "token: two tokens sharing an alias stay on separate rows."
    )

    if markdown:
        # §117. Prints; it does not write a file. `payees.md` is gone — the
        # Payees page shows every token with its alias, category, counts and
        # dates, and lets you change two of them, which is all the file was for
        # plus the part it could not do. What is left here is a table you can
        # read in a terminal during a recovery, and redirect if you want one.
        lines = [f"# payee tokens ({len(rows)} distinct)", "", note, ""]
        lines.append("| " + " | ".join(headers) + " |")
        lines.append("|" + "|".join("---" for _ in headers) + "|")
        for i, r in enumerate(rows[:top], 1):
            lines.append("| " + " | ".join(c or "" for c in cells(i, r)) + " |")
        print("\n".join(lines))
        return

    table = Table(header_style="bold", title=f"payee tokens ({len(rows)} distinct)")
    for head in headers:
        table.add_column(head, justify="right" if head in
                         ("#", "Len", "Txns", "Withdrawn", "Deposited", "Total") else "left")
    for i, r in enumerate(rows[:top], 1):
        table.add_row(*cells(i, r))
    console.print(table)
    console.print(f"\n[dim]{note}[/dim]")


@app.command(name="web-password")
def web_password(
    username: str = typer.Option(..., prompt=True),
    password: str = typer.Option(
        ..., prompt=True, hide_input=True, confirmation_prompt=True
    ),
    print_only: bool = typer.Option(
        False, "--print-only", help="emit the lines instead of writing .env"
    ),
) -> None:
    """Set web UI credentials in config/web-auth.json. Recovery path. SPEC §16.

    Writes the file itself rather than printing something to paste: a hash
    wrapped by a terminal and pasted back as two lines produced a value with a
    newline through the middle, which surfaced only as "login failed".

    JSON outside .env, so the `$` in a scrypt hash is just a character, and so
    the UI's own change-password form can write it without touching .env.

    **Preserves TOTP enrolment.** Resetting a forgotten password must not also
    destroy the second factor — that turns one recoverable problem into two.
    Use `passbook web-totp --reset` when it is the phone that is gone.
    """
    from . import webauth

    hashed = webauth.hash_password(password)

    if print_only:
        console.print(f'{{"username": "{username}", "password_hash": "{hashed}"}}')
        return

    auth = webauth.load()
    auth.username = username
    auth.password_hash = hashed
    path = webauth.save(auth)

    console.print(f"[green]wrote[/green] {path} (mode 600)")
    console.print(f"  username      {username}")
    console.print("  password_hash [dim]<not shown>[/dim]")
    if auth.totp_enrolled:
        console.print(
            f"  TOTP          [green]still enrolled[/green] "
            f"({auth.backup_codes_left} backup code(s) left)"
        )
    else:
        console.print("  TOTP          [yellow]not enrolled[/yellow] — required at next sign-in")
    console.print(
        "\nThe UI reads this file on each request, so no restart is needed.\n"
        f"[dim]{webauth.WEB_AUTH_FILE} is gitignored and is captured by `make backup`.[/dim]"
    )


@app.command(name="web-totp")
def web_totp(
    reset: bool = typer.Option(False, "--reset", help="clear TOTP so the next sign-in re-enrols"),
    forget_devices: bool = typer.Option(
        False, "--forget-devices", help="revoke every remembered device"
    ),
) -> None:
    """Inspect or reset the second factor. The way back in when the phone is gone.

    A lost authenticator is the failure mode TOTP creates, so it needs a door
    that does not depend on the thing that was lost. There are two: a backup
    code from the browser, and this, which needs a shell on the host. `--reset`
    clears the secret and the backup codes; the next sign-in enrols again with a
    fresh QR.

    Deliberately does **not** print the secret. Reading it out would let anyone
    who can run this command mint codes silently and indefinitely; resetting it
    is visible the next time you sign in.
    """
    from . import webauth

    auth = webauth.load()
    if not auth.configured:
        err.print("[red]no credentials configured[/red] — run `make web-password` first")
        raise typer.Exit(1)

    if reset:
        auth.totp_secret = None
        auth.totp_enrolled_at = None
        auth.totp_last_counter = None
        auth.backup_codes = []
        webauth.forget_devices(auth)
        webauth.save(auth)
        console.print("[green]TOTP cleared.[/green] The next sign-in will enrol a new secret,")
        console.print("issue eight fresh backup codes, and revoke every remembered device.")
        return

    if forget_devices:
        count = webauth.forget_devices(auth)
        webauth.save(auth)
        console.print(f"[green]revoked[/green] {count} remembered device(s)")
        return

    webauth.prune_devices(auth)
    console.print(f"  username        {auth.username}")
    console.print(
        "  TOTP            "
        + (
            f"[green]enrolled[/green] {auth.totp_enrolled_at or ''}"
            if auth.totp_enrolled
            else "[yellow]not enrolled[/yellow]"
        )
    )
    console.print(f"  backup codes    {auth.backup_codes_left} of {webauth.BACKUP_CODE_COUNT} left")
    console.print(f"  remembered      {len(auth.devices)} device(s)")
    if auth.totp_enrolled and auth.backup_codes_left == 0:
        err.print(
            "\n[yellow]No backup codes left.[/yellow] Losing the phone now means "
            "`passbook web-totp --reset` on this host is the only way back in."
        )


@app.command(name="sync-age")
def sync_age() -> None:
    """How long since the last successful sync. No network, no .env needed.

    Split out so `make up` can surface it: the reminder is worthless if it only
    appears in a command you run when you have already remembered.
    """
    sync_staleness()


# --- accounts ----------------------------------------------------------------

accounts_app = typer.Typer(help="The account registry. SPEC §21.")
app.add_typer(accounts_app, name="accounts")


@accounts_app.command("list")
def accounts_list() -> None:
    """Show the registry. Masked — a full account number never reaches a log."""
    registry = load_accounts()
    if not registry:
        console.print(
            "no accounts registered.\n"
            "The first statement you upload registers its own account (§21.3); "
            "nothing to do until then."
        )
        return
    table = Table(box=None, pad_edge=False)
    for column in ("slug", "bank", "account", "the ledger asset account", "label"):
        table.add_column(column)
    for account in registry:
        table.add_row(
            account.slug, account.bank, account.masked, account.asset_account, account.label or "—"
        )
    console.print(table)
    console.print(
        f"\n{len(registry)} account(s). `slug` is the external_id namespace and is "
        "immutable once rows exist (§21.1)."
    )


@accounts_app.command("add")
def accounts_add(
    statement: Path = typer.Argument(..., help="a statement for the account to add"),
    asset_account: str = typer.Option(
        None, "--asset-account", help="the asset account to post into"
    ),
    slug: str = typer.Option(None, help="external_id namespace; defaults to <bank>-<last4>"),
    label: str = typer.Option(None, help="what the switcher shows"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Register an account from one of its statements. SPEC §21.3.

    Read from the statement rather than typed: the account number is in the file
    (§6.3), and a hand-typed one that is wrong by a digit would give the account
    its own namespace and its own ledger, silently, forever.
    """
    _setup_logging(verbose)
    meta, _, _ = _read(statement)
    registry = load_accounts()

    existing = next(
        (a for a in registry if a.account_number.strip() == meta.account_number.strip()), None
    )
    if existing:
        console.print(
            f"already registered as [bold]{existing.slug}[/bold] "
            f"({existing.masked} -> {existing.asset_account!r})"
        )
        return

    settings = load_settings()
    target = (asset_account or "").strip()
    if not target:
        with open_ledger(settings) as store:
            names = [a["name"] for a in store.asset_accounts()]
        taken = {a.asset_account for a in registry}
        free = [n for n in names if n not in taken]
        if len(free) == 1:
            target = free[0]
            console.print(f"using the only unclaimed asset account: {target!r}")
        else:
            # Never guessed. Two registry accounts sharing one asset account
            # merge whatever the registry says, and `doctor` has refused to
            # guess between several since §7.2.
            err.print(
                "[red]--asset-account is required.[/red] the ledger has "
                f"{len(names)} asset account(s): {', '.join(repr(n) for n in names)}"
                + (f"; already claimed: {', '.join(sorted(taken))}" if taken else "")
            )
            raise typer.Exit(5)

    chosen_slug = (slug or default_slug("canara", meta.account_number)).strip()
    account = Account(
        slug=chosen_slug,
        bank="canara",
        account_number=meta.account_number.strip(),
        asset_account=target,
        label=(label or meta.account_name).strip()[:40],
    )
    try:
        save_accounts([*registry, account])
    except RegistryError as exc:
        err.print(f"[red]refused:[/red] {exc}")
        raise typer.Exit(5) from exc

    console.print(
        f"registered [bold]{account.slug}[/bold] — {account.masked} -> "
        f"{account.asset_account!r}\n"
        f"  external_ids will be {account.slug}-<txn_id>\n"
        f"  statements archive to archive/{account.slug}/\n"
        "  payee_aliases.yaml and rules.yaml stay SHARED across accounts (§21.5)"
    )


@app.command()
def doctor(verbose: bool = typer.Option(False, "-v", "--verbose")) -> None:
    """Check .env, the ledger reachability, the token, and the asset account.

    Run this before pushing anything. SPEC §7.3.
    """
    _setup_logging(verbose)
    settings = load_settings()
    problems = 0

    def ok(msg):
        console.print(f"  [green]ok[/green]    {msg}")

    def warn(msg):
        console.print(f"  [yellow]warn[/yellow]  {msg}")

    def bad(msg):
        nonlocal problems
        problems += 1
        console.print(f"  [red]FAIL[/red]  {msg}")

    console.print("[bold]configuration[/bold]")
    if settings.passbook_account_number:
        ok("PASSBOOK_ACCOUNT_NUMBER is set")
    else:
        bad("PASSBOOK_ACCOUNT_NUMBER is not set — the §6.7 safety assertion cannot run")

    sync_staleness()

    console.print("\n[bold]ledger[/bold]")
    if problems:
        console.print("  [dim]skipped — fix the configuration above first[/dim]")
        raise typer.Exit(1)

    # No credential to check. The ledger is passbook's own, on the database the
    # stack already runs, so the only question worth asking is whether it
    # answers — and the way to find that out is to ask it.
    try:
        with open_ledger(settings) as store:
            accounts = store.asset_accounts()
            names = [a["name"] for a in accounts]
            ok("the ledger answers")
            if not accounts:
                warn("no asset accounts yet — registering a statement creates one")
            else:
                ok(f"{len(accounts)} asset account(s): {', '.join(repr(n) for n in names)}")

            configured = settings.passbook_asset_account
            if configured and configured not in names:
                bad(f"PASSBOOK_ASSET_ACCOUNT {configured!r} is not one of {names}")
            elif configured:
                match = next(a for a in accounts if a["name"] == configured)
                currency = match.get("currency")
                ok(f"target account {configured!r} exists (currency {currency})")
                if currency != "INR":
                    bad(f"target account currency is {currency}, expected INR")
    except LedgerError as exc:
        bad(str(exc))

    # --- the ledger's contents, which is a different question from whether it
    #     answers: everything above can pass while it holds a third of the rows.
    registry = load_accounts(settings=settings)
    if registry:
        try:
            with open_ledger(settings) as store:
                for entry in registry:
                    console.print(
                        f"\n[bold]ledger integrity[/bold] — {entry.slug} ({entry.masked})"
                    )
                    _print_verdict(
                        service.verify_ledger(store, entry),
                        ok=ok, warn=warn, bad=bad,
                    )
        except LedgerError as exc:
            bad(f"could not verify the ledger: {exc}")

    console.print()
    if problems:
        console.print(f"[red]{problems} problem(s).[/red] Fix these before pushing.")
        raise typer.Exit(1)
    console.print("[green]all checks passed — safe to push.[/green]")


def _require_pushable(meta):
    """Shared preflight for push/sync. Returns (settings, Account).

    §21.2: the question is which of my accounts this statement is for. A
    statement for an account the registry does not know is refused here, before
    anything is posted — the same guarantee §6.7 gave, now with more than one
    possible answer.
    """
    settings = load_settings()
    try:
        with open_ledger(settings) as store:
            account = service.resolve_account(meta, settings, store=store)
    except UnknownAccount as exc:
        err.print(f"[red]unregistered account:[/red] {exc}")
        err.print("Add it with: [bold]passbook accounts add[/bold]")
        raise typer.Exit(4) from exc
    except AccountMismatch as exc:
        err.print(f"[red]account assertion failed:[/red] {exc}")
        raise typer.Exit(4) from exc
    if not account.asset_account:
        err.print(
            "[red]no asset account for this account.[/red] "
            "Run `passbook doctor` to list them, then `passbook accounts add`."
        )
        raise typer.Exit(5)
    return settings, account


def _report(result, parsed: int, warnings: list[str]) -> None:
    console.print(
        f"\nrows parsed        {parsed}\n"
        f"pushed             {result.pushed}\n"
        f"duplicates skipped {result.duplicates}\n"
        f"failed             {result.failed}\n"
        f"warnings           {len(warnings)}"
    )
    for txn_id, message in result.failures[:10]:
        console.print(f"  [red]fail[/red] {txn_id}: {message}")


def _archived_statements_paths(archive: Path = Path("archive")) -> list[Path]:
    """Every archived statement, in a stable order. What a resume pushes back."""
    if not archive.is_dir():
        return []
    return sorted(p for p in archive.rglob("*") if p.is_file() and not p.name.startswith("."))


# --- ledger integrity --------------------------------------------------------


def _print_verdict(verdict, *, ok, warn, bad) -> None:
    """One renderer for the CLI and for `doctor`, so the wording cannot drift."""
    for check in verdict.checks:
        line = f"{check.name:<16} {check.detail}"
        if check.ok is True:
            ok(line)
        elif check.ok is False:
            bad(line)
        else:
            warn(line + "  [not checked]")


@app.command(name="verify-ledger")
def verify_ledger_command(
    account: str = typer.Option(None, help="asset account; defaults to PASSBOOK_ASSET_ACCOUNT"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Check the LIVE ledger against the statements that built it.

    The gap this exists to close: the continuity invariant validates a *file* at
    parse time, and for a long time nothing validated the ledger. A purge plus
    an interrupted re-push once left 21 of 93 rows with a self-consistent
    balance, and every existing check passed for seven hours.

    Exits non-zero if any check fails, so it can gate a script.
    """
    _setup_logging(verbose)
    settings = load_settings()
    if account:
        settings = settings.model_copy(update={"passbook_asset_account": account})
    if not settings.passbook_asset_account:
        err.print("[red]PASSBOOK_ASSET_ACCOUNT is not set.[/red] Run `passbook doctor`.")
        raise typer.Exit(5)

    registry = load_accounts(settings=settings)
    if account:
        registry = [a for a in registry if a.slug == account or a.asset_account == account]
        if not registry:
            err.print(f"[red]no registered account matching {account!r}[/red]")
            raise typer.Exit(5)
    if not registry:
        err.print("[red]no accounts registered.[/red] Run `passbook accounts list`.")
        raise typer.Exit(5)

    # Every account, not just the first: §21.6. A second account whose rows never
    # arrived is exactly as invisible as the first one's were during §19.
    verdicts = []
    try:
        with open_ledger(settings) as store:
            for entry in registry:
                verdicts.append(
                    (entry, service.verify_ledger(store, entry))
                )
    except LedgerError as exc:
        err.print(f"[red]the ledger did not answer:[/red] {exc}")
        raise typer.Exit(2) from exc

    for entry, verdict in verdicts:
        console.print(
            f"[bold]ledger integrity[/bold] — {entry.slug} ({entry.masked}, "
            f"{entry.asset_account!r})"
        )
        _print_verdict(
            verdict,
            ok=lambda m: console.print(f"  [green]ok[/green]    {m}"),
            warn=lambda m: console.print(f"  [yellow]warn[/yellow]  {m}"),
            bad=lambda m: console.print(f"  [red]FAIL[/red]  {m}"),
        )
        console.print()

    verdict = service.LedgerVerdict([c for _, v in verdicts for c in v.checks])
    if verdict.failed:
        console.print(
            f"\n[red bold]{len(verdict.failed)} check(s) failed.[/red bold] "
            "Read the failing line: it names the figure, the drift or the remedy. "
            "Take `make backup` before changing anything (SPEC §19.5)."
        )
        raise typer.Exit(7)
    if verdict.unchecked:
        console.print(f"\n[yellow]{verdict.headline}[/yellow]")
    else:
        console.print(f"\n[green]{verdict.headline}[/green]")


@app.command()
def push(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    dry_run: bool = typer.Option(False, "--dry-run", help="print payloads, post nothing"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Write one statement into the ledger."""
    _setup_logging(verbose)
    meta, transactions, warnings = _read(file)
    settings, account = _require_pushable(meta)

    if dry_run:
        console.print(
            f"[bold]dry run[/bold] — {len(transactions)} rows for "
            f"{meta.masked_account}, routed to {account.slug} "
            f"({account.asset_account!r}). Nothing is written.\n"
        )
        rules = load_rules()
        for txn in transactions[:3]:
            console.print_json(
                json.dumps(
                    build_split(
                        txn, account, rules=rules, threshold=settings.large_txn_threshold
                    ),
                    default=str,  # a date and a Decimal are not JSON by themselves
                )
            )
        if len(transactions) > 3:
            console.print(f"[dim]... and {len(transactions) - 3} more[/dim]")
        kinds = defaultdict(int)
        for txn in transactions:
            kinds[build_split(txn, account)["kind"]] += 1
        console.print(f"\ntypes: {dict(kinds)}")
        console.print(f"rows parsed        {len(transactions)}\nwould push         {len(transactions)}")
        return

    with open_ledger(settings) as store:
        with console.status(f"pushing {len(transactions)} transactions..."):
            result = push_transactions(store, transactions, account)
    _report(result, len(transactions), warnings)
    if not result.ok:
        raise typer.Exit(6)


@app.command()
def sync(
    inbox: Path = typer.Option(Path("inbox"), help="directory to process"),
    archive: Path = typer.Option(Path("archive"), help="where to move processed files"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Process every statement in inbox/, archiving each on success. SPEC §7.3.

    Safely re-runnable: a file is archived only after a successful push, and a
    failure leaves it in place and exits non-zero.
    """
    _setup_logging(verbose)
    # Before pushing: this reports the gap being closed, not the one after.
    sync_staleness()

    files = sorted(p for p in inbox.glob("*") if p.is_file() and not p.name.startswith("."))
    if not files:
        console.print(f"\nnothing to do — {inbox}/ is empty")
        return

    failures = 0
    for path in files:
        console.print(f"\n[bold]{path.name}[/bold]")
        meta, transactions, warnings = _read(path)
        settings, account = _require_pushable(meta)

        if dry_run:
            console.print(
                f"  dry run: would push {len(transactions)} into "
                f"{account.asset_account!r} as {account.slug}"
            )
            continue

        with open_ledger(settings) as store:
            with console.status(f"pushing {len(transactions)}..."):
                result = push_transactions(store, transactions, account)
        _report(result, len(transactions), warnings)

        if result.ok:
            target = archive / account.slug / f"{meta.period_to:%Y-%m}"
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(target / path.name))
            console.print(f"  archived -> {target / path.name}")
        else:
            failures += 1
            console.print(f"  [red]left in {inbox}/[/red] — {result.failed} failed")

    if failures:
        raise typer.Exit(6)


@app.command()
def resync(
    confirm: bool = typer.Option(False, "--confirm", help="actually write; omit for a dry run"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Write the current config onto rows already in the ledger. SPEC §23.

    Aliases and rules are applied at push time, so editing `config/` leaves rows
    already pushed showing the names they were pushed with. This rewrites those
    rows in place — description, category, the payee account on the other side,
    and the tags your rules derive — with
    `PUT /api/v1/transactions/<group>`.

    **Not a purge.** Nothing is deleted, no dump is required, and running it
    twice is the same as running it once. What it cannot do is create a row that
    is missing from the ledger or correct an amount: those come from the statement,
    so they need `passbook sync` or a re-push. Anything left over is re-read from
    The ledger and reported rather than assumed away.

    Dry run unless --confirm.
    """
    _setup_logging(verbose)
    settings = load_settings()

    with open_ledger(settings) as store:
        changes, considered = service.reapply_preview(store, settings)

        if considered == 0:
            # A green "0 of 0 match" is the exact shape of §23.1's bug.
            console.print(
                "[yellow]nothing was compared[/yellow] — no row in the ledger carries an "
                "external_id matching a statement in archive/. That is an unanswered "
                "question, not a pass. Check `passbook verify-ledger` and that "
                "PASSBOOK_ASSET_ACCOUNT names the account the rows were pushed into."
            )
            raise typer.Exit(7)

        if not changes:
            console.print(
                f"[green]nothing to do[/green] — all {considered} row(s) in the ledger "
                "already match the current config."
            )
            return

        console.print(
            f"[bold]{'RESYNC' if confirm else 'dry run'}[/bold] — "
            f"{len(changes)} of {considered} row(s) differ\n"
        )
        for change in changes[:10]:
            console.print(f"  [dim]{change.date}  {change.external_id}[/dim]")
            if change.name_changed:
                console.print(
                    f"    name      {change.old_description!r} -> "
                    f"[bold]{change.new_description!r}[/bold]"
                )
            if change.category_changed:
                console.print(
                    f"    category  {change.old_category or '(none)'!r} -> "
                    f"[bold]{change.new_category or '(none)'!r}[/bold]"
                )
            if change.counterparty_changed:
                console.print(
                    f"    payee a/c {change.old_counterparty!r} -> "
                    f"[bold]{change.new_counterparty!r}[/bold]"
                )
            if change.tags_changed:
                console.print(
                    f"    tags      {list(change.old_tags)} -> "
                    f"[bold]{list(change.new_tags)}[/bold]"
                )
        if len(changes) > 10:
            console.print(f"  [dim]... and {len(changes) - 10} more[/dim]")

        if not confirm:
            console.print(
                "\n[yellow]Dry run — nothing written.[/yellow] "
                "Re-run with --confirm to apply."
            )
            return

        result = service.sync_ledger(store, changes)
        for external_id, message in result.failures[:10]:
            console.print(f"  [red]fail[/red] {external_id}: {message}")

        # Re-read. "12 requests returned 200" is not the same claim as "12 rows
        # in the ledger now match", and only the second one is worth printing.
        remaining, _ = service.reapply_preview(store, settings)

    console.print(
        f"\n[green]{result.updated} updated[/green], {result.failed} failed, "
        f"{len(remaining)} still differing."
    )
    if remaining:
        console.print(
            "[yellow]Rows an update cannot fix[/yellow] — a row missing from the ledger, or "
            "one whose amount or date is wrong. Those need a re-push: "
            "`passbook purge --confirm` then `passbook sync`."
        )
        raise typer.Exit(7)
    if not result.ok:
        raise typer.Exit(7)


#: How old the newest dump may be before `dedupe` refuses to delete. Six hours:
#: long enough that taking one, reading the plan and confirming is a single
#: sitting, short enough that the undo is of the ledger being changed rather
#: than of last week's.
BACKUP_MAX_AGE_MINUTES = 6 * 60


@app.command()
def reminder(
    email: bool = typer.Option(False, "--email", help="mail the invite to your calendar"),
    ics: Path = typer.Option(None, "--ics", help="write the calendar file here instead"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Show the statement reminder, mail it, or write it as a calendar file. SPEC §24.

    passbook does not fire this and deliberately does not try. WSL2 stops when
    Windows sleeps and there is no systemd here, so a reminder owned by this
    machine is one that will not arrive. `--ics` writes an RFC 5545 file: import
    it into Google Calendar and the calendar does the reminding, on a device
    that is awake. Switch that event's notification to "Email" if you want it
    mailed — the calendar sends it, so no SMTP credential lives here.

    Edit the schedule on the Reminder page, or in `config/reminder.yaml`.
    """
    _setup_logging(verbose)
    schedule = reminders.load()

    state = "on" if schedule.enabled else "[yellow]off[/yellow]"
    console.print(f"reminder {state} — [bold]{schedule.label}[/bold] ({reminders.TZID})")
    console.print(f"  alert    {schedule.lead_minutes} minute(s) before")
    console.print("  steps    " + "; ".join(reminders.STEPS))
    console.print()
    for moment in reminders.next_occurrences(schedule, datetime.now(), 5):
        console.print(f"  [dim]{moment:%a %d %b %Y  %H:%M}[/dim]")

    if email:
        settings = load_settings()
        try:
            to = reminders.send_invite(schedule, settings)
        except reminders.SendFailed as exc:
            err.print(f"\n[red]{exc}[/red]")
            raise typer.Exit(5) from exc
        console.print(
            f"\n[green]invite sent[/green] to {reminders._mask_email(to)} — accept it once "
            "and your calendar handles every reminder after that."
        )
        return

    if ics is None:
        console.print(
            "\nMail it straight to your calendar with `passbook reminder --email`, or "
            "write the file with `passbook reminder --ics reminder.ics`."
        )
        return

    ics.parent.mkdir(parents=True, exist_ok=True)
    # newline="" so the CRLF pairs RFC 5545 requires survive the write; Python
    # would otherwise translate them and some importers reject the result.
    with ics.open("w", encoding="utf-8", newline="") as fh:
        fh.write(reminders.to_ics(schedule))
    console.print(f"\n[green]wrote[/green] {ics} — import it into your calendar.")


@app.command()
def purge(
    account: str = typer.Option(None, help="asset account name; defaults to PASSBOOK_ASSET_ACCOUNT"),
    confirm: bool = typer.Option(False, "--confirm", help="actually delete; omit for a dry run"),
    yes: bool = typer.Option(False, "--yes", help="skip the interactive prompt"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Delete transactions passbook wrote into an asset account.

    Dry run unless --confirm. Only rows carrying an `external_id` are touched,
    so an opening balance is excluded structurally rather than by a date guard.

    Rarely what you want: a re-push skips rows already in the ledger by
    identity, and `passbook resync` applies config to existing rows in place.

    There is no `--resume`, and that is not a feature that was dropped. The
    delete used to be thousands of separate requests that could die halfway, so
    the intent was written to a file first and finished later. It is one
    statement in one transaction now.
    """
    _setup_logging(verbose)
    settings = load_settings()
    target = account or settings.passbook_asset_account
    if not target:
        err.print("[red]no account given[/red] and PASSBOOK_ASSET_ACCOUNT is unset.")
        raise typer.Exit(5)

    with open_ledger(settings) as store:
        known = {a["name"] for a in store.asset_accounts()}
        if target not in known:
            err.print(f"[red]no asset account named {target!r}[/red]; have {sorted(known)}")
            raise typer.Exit(5)

        candidates, protected = find_candidates(store, target)

        if not candidates:
            console.print(f"nothing to purge on {target!r} ({len(protected)} protected).")
            return

        total = sum(c.amount for c in candidates)
        dates = sorted(c.date for c in candidates)
        console.print(
            f"[bold]{'PURGE' if confirm else 'dry run'}[/bold] on {target!r}\n"
            f"  deletable (has external_id)  {len(candidates)}\n"
            f"  protected (no external_id)   {len(protected)}"
            + (f"  -> {', '.join(protected[:3])}" if protected else "")
            + f"\n  date range                   {dates[0]} .. {dates[-1]}\n"
            f"  total value                  {total:,}\n"
        )
        for c in candidates[:3]:
            console.print(f"  [dim]{c.date}  {c.external_id}  {c.description[:38]:<38} {c.amount:>10,}[/dim]")
        if len(candidates) > 3:
            console.print(f"  [dim]... and {len(candidates) - 3} more[/dim]")

        if not confirm:
            console.print(
                "\n[yellow]Dry run — nothing deleted.[/yellow] "
                "Re-run with --confirm to delete."
            )
            return

        console.print(
            f"\n[red bold]This permanently deletes {len(candidates)} transactions "
            f"from {target!r}.[/red bold] Take `make backup` first if you have not."
        )
        if not yes and not typer.confirm(f"Delete {len(candidates)} transactions?"):
            console.print("aborted.")
            raise typer.Exit(1)

        with console.status(f"deleting {len(candidates)}..."):
            result = purge_transactions(store, candidates)

    console.print(
        f"\ndeleted            {result.deleted}\n"
        f"already gone       {result.already_gone}\n"
        f"failed             {result.failed}"
    )
    for external_id, message in result.failures[:10]:
        console.print(f"  [red]fail[/red] {external_id}: {message}")
    console.print(
        "\n[yellow]The ledger is now short until the statements are pushed "
        "back.[/yellow] Run `passbook sync`; `passbook verify-ledger` reports "
        "the gap until then."
    )
    if not result.ok:
        raise typer.Exit(6)


# --- migrations --------------------------------------------------------------


def _migration_context(store, settings, registry):
    """Everything a migration is allowed to touch.

    The dangerous half is supplied as callables so a migration cannot grow its
    own copy of the rebuild path — `purge_and_repush` is the same code
    `passbook purge --confirm --yes` runs, followed by the same push `passbook
    sync` runs.
    """
    from . import migrate

    def purge_and_repush(account) -> None:
        known = {a["name"] for a in store.asset_accounts()}
        if account.asset_account not in known:
            raise RuntimeError(f"no asset account named {account.asset_account!r}")
        candidates, _ = find_candidates(store, account.asset_account)
        statements = _archived_statements_paths()
        if candidates and not statements:
            # Deleting rows this machine cannot rebuild is not a migration; it
            # is data loss with a progress bar.
            raise RuntimeError(
                f"{account.slug}: {len(candidates)} row(s) in the ledger but "
                "nothing in archive/ to push back. Re-download the statements "
                "first."
            )
        if candidates:
            console.print(f"  {account.slug}: purging {len(candidates)} row(s)")
            result = purge_transactions(store, candidates)
            if not result.ok:
                raise RuntimeError(f"{account.slug}: {result.failed} delete(s) failed")

        # This account's statements, not every statement: `statements_for`
        # attributes each archived file by the account number it carries, so a
        # two-account archive cannot push one account's rows into the other.
        mine = service.statements_for(account, service.archived_statements())
        console.print(f"  {account.slug}: re-pushing {len(mine)} statement(s)")
        for parsed in mine:
            outcome = service.push_statement(
                service.parse_statement(parsed.path), settings, store, account=account
            )
            if outcome.failed:
                raise RuntimeError(
                    f"{account.slug}: {outcome.failed} row(s) failed to write back"
                )

    return migrate.Context(
        settings=settings,
        store=store,
        registry=registry,
        say=lambda message: console.print(f"  {message}"),
        purge_and_repush=purge_and_repush,
        statement_paths=lambda _account: _archived_statements_paths(),
    )


@app.command()
def upgrade(
    check: bool = typer.Option(False, "--check", help="report what is pending, change nothing"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Apply pending schema migrations. SPEC §22.2 — normally via `make upgrade`.

    `git pull` is silent, and Phase 14 changed the shape of every `external_id`
    in the ledger. Someone who pulled that and ran `make sync` would have got a
    ledger holding two incompatible id forms with nothing on screen ever having
    said a migration existed.

    **What is pending is read from the ledger, not from the version file.** The
    marker is a record; the rows are the authority. `--check` exits 3 when work
    is outstanding, so a script can gate on it.
    """
    from . import migrate

    _setup_logging(verbose)
    settings = load_settings()
    target = migrate.schema_version()
    recorded = migrate.recorded_version()

    console.print(
        f"[bold]schema[/bold]  this checkout: {target}   "
        f"this install: {recorded if recorded is not None else 'unrecorded'}"
    )

    if not settings.passbook_asset_account:
        err.print(
            "[red]PASSBOOK_ASSET_ACCOUNT is not set.[/red] "
            "Migrations read the live ledger to decide what is pending, so there "
            "is nothing they can honestly say without it. Run `passbook doctor`."
        )
        raise typer.Exit(5)

    registry = load_accounts(settings=settings)
    with open_ledger(settings) as store:
        ctx = _migration_context(store, settings, registry)
        outstanding = migrate.pending(ctx)

        if not outstanding:
            console.print(f"[green]up to date[/green] — nothing pending at schema {target}.")
            migrate.record_version(target)
            return

        console.print(f"\n[yellow]{len(outstanding)} migration(s) pending:[/yellow]")
        for step, reason in outstanding:
            console.print(f"  [bold]{step.version:03d} {step.name}[/bold]")
            console.print(f"        {step.description}")
            console.print(f"        [yellow]why now:[/yellow] {reason}")
        console.print()

        if check:
            console.print(
                "Run [bold]make upgrade[/bold] to apply. It takes a database dump "
                "first and refuses without one."
            )
            raise typer.Exit(3)

        # A dump is a precondition, not advice — the same shape §18.7 put in
        # front of the re-apply button, for the same reason: this deletes rows.
        dump = ops.newest_dump()
        if dump is None:
            err.print(
                "[red]no database dump in backups/.[/red] A migration re-pushes the "
                "ledger, which starts with a delete. Run `make backup` first."
            )
            raise typer.Exit(9)
        name, age = dump
        if age > ops.REAPPLY_DUMP_MAX_AGE_MINUTES:
            err.print(
                f"[red]the newest dump ({name}) is {age} minutes old.[/red] A dump "
                f"older than {ops.REAPPLY_DUMP_MAX_AGE_MINUTES} minutes is a dump of "
                "some earlier ledger, not of the one about to be deleted. Run "
                "`make backup` and try again."
            )
            raise typer.Exit(9)
        console.print(f"recovering from, if it comes to that: [bold]{name}[/bold] ({age}m old)\n")

        for step, _ in outstanding:
            console.print(f"[bold]{step.version:03d} {step.name}[/bold]")
            step.run(ctx)
            problem = step.verify(ctx)
            if problem:
                err.print(
                    f"[red]{step.name} did not finish:[/red] {problem}\n"
                    f"Nothing recorded. Recover from {name} if the ledger is "
                    "short — `passbook verify-ledger` will say which it is."
                )
                raise typer.Exit(7)
            console.print("  [green]verified[/green]")

        verdict = service.LedgerVerdict(
            [
                item
                for entry in registry
                for item in service.verify_ledger(store, entry).checks
            ]
        )

    console.print("\n[bold]ledger integrity[/bold]")
    _print_verdict(
        verdict,
        ok=lambda m: console.print(f"  [green]ok[/green]    {m}"),
        warn=lambda m: console.print(f"  [yellow]warn[/yellow]  {m}"),
        bad=lambda m: console.print(f"  [red]FAIL[/red]  {m}"),
    )
    if verdict.failed:
        err.print(
            "\n[red bold]not recording the new version[/red bold] — the ledger does "
            "not verify. §20 passing is what marks a migration done, not a function "
            "returning (§19.7)."
        )
        raise typer.Exit(7)

    migrate.record_version(target)
    console.print(f"\n[green]upgraded to schema {target}[/green] and verified.")


if __name__ == "__main__":
    app()
