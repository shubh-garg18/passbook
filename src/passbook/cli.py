"""Typer entrypoint. SPEC §7.3.

`parse` and `payees` are read-only and make no network calls. `doctor`, `push`,
`sync`, `bootstrap` and `purge` talk to Firefly; the last three write.
"""

import json
import logging
import shutil
from collections import defaultdict
from datetime import datetime, timezone
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
    GENERATED_COLUMNS,
    alias_drift,
    parse_payees_table,
    load_payee_aliases,
    load_settings,
    token_expiry,
)
from .firefly.bootstrap import RULES_FILE, load_bills, load_rules
from .firefly.bootstrap import bootstrap as bootstrap_rules
from .firefly.client import FireflyClient, FireflyError
from .firefly.purge import find_candidates
from .firefly.purge import purge as purge_transactions
from .firefly.push import build_payload, push_transactions
from .loaders import load as load_statement
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

app = typer.Typer(add_completion=False, help="Canara Bank -> Firefly III ingest pipeline.")

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
    push, so it records what actually reached Firefly rather than what was
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
    """SPEC §6.7. Returns a status line; exits non-zero on a real mismatch."""
    configured = load_settings().passbook_account_number
    try:
        assert_account(meta, configured)
    except AccountMismatch as exc:
        if not configured:
            # Read-only command: warn rather than refuse. The refusal that
            # matters is on push, which is Phase 3.
            return f"[yellow]unverified[/yellow] ({exc})"
        err.print(f"[red]account assertion failed:[/red] {exc}")
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
def payees(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, readable=True),
    top: int = typer.Option(25, "--top", "-n", help="how many to show"),
    markdown: bool = typer.Option(
        False, "--markdown", help="emit a markdown table instead of a box table"
    ),
    out: Path = typer.Option(
        None, "--out", help="write to this file; refuses to overwrite without --force"
    ),
    force: bool = typer.Option(False, "--force", help="allow --out to overwrite"),
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

    if markdown or out:
        # The Alias column is GENERATED from payee_aliases.yaml, which the UI
        # writes. Treating it as hand-maintained made every UI edit look like
        # drift; generating it makes drift impossible by construction.
        # Anything the operator added beyond the generated columns is carried
        # across, so annotating the file is still safe.
        extra_headers: list[str] = []
        extra_rows: dict[str, dict[str, str]] = {}
        if out is not None and out.exists():
            existing_header, existing_rows = parse_payees_table(out)
            if existing_header:
                extra_headers = [
                    h for h in existing_header if h.lower() not in GENERATED_COLUMNS
                ]
                extra_rows = existing_rows
            elif not force:
                err.print(
                    f"[red]{out} exists but is not a payees table[/red] — refusing to "
                    "overwrite. Pass --force, or --out a different path."
                )
                raise typer.Exit(1)

        all_headers = list(headers) + extra_headers
        lines = [f"# payee tokens ({len(rows)} distinct)", "", note, ""]
        if extra_headers:
            lines.append(
                f"Columns {', '.join(extra_headers)} are yours and are preserved; "
                "the rest are regenerated from config/payee_aliases.yaml."
            )
            lines.append("")
        lines.append("| " + " | ".join(all_headers) + " |")
        lines.append("|" + "|".join("---" for _ in all_headers) + "|")
        for i, r in enumerate(rows[:top], 1):
            generated = list(cells(i, r))
            kept = [extra_rows.get(r[0], {}).get(h, "") for h in extra_headers]
            lines.append("| " + " | ".join(c or "" for c in generated + kept) + " |")
        text = "\n".join(lines) + "\n"

        if out is None:
            print(text, end="")
            return
        out.write_text(text, encoding="utf-8")
        console.print(
            f"wrote {out} ({len(rows)} tokens"
            + (f", preserved {len(extra_headers)} of your column(s)" if extra_headers else "")
            + ")"
        )
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
    for column in ("slug", "bank", "account", "firefly asset account", "label"):
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
        None, "--asset-account", help="the Firefly asset account to post into"
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
        if not settings.firefly_token:
            err.print("[red]--asset-account is required[/red] (no FIREFLY_TOKEN to list them).")
            raise typer.Exit(5)
        with FireflyClient(settings.firefly_url, settings.firefly_token) as client:
            names = [a["attributes"]["name"] for a in client.asset_accounts()]
        taken = {a.asset_account for a in registry}
        free = [n for n in names if n not in taken]
        if len(free) == 1:
            target = free[0]
            console.print(f"using the only unclaimed asset account: {target!r}")
        else:
            # Never guessed. Two accounts sharing one Firefly asset account merge
            # in Firefly whatever the registry says, and `doctor` has refused to
            # guess between several since §7.2.
            err.print(
                "[red]--asset-account is required.[/red] Firefly has "
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
    """Check .env, Firefly reachability, the token, and the asset account.

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

    token = (settings.firefly_token or "").strip()
    if not token:
        bad("FIREFLY_TOKEN is not set")
    elif token.count(".") != 2:
        bad(
            "FIREFLY_TOKEN is not a JWT (expected ~1000 chars and 2 dots). "
            "The 'Command line token' on the Profile page is a different "
            "credential — take the one from Options -> Remote access and tokens."
        )
    else:
        ok(f"FIREFLY_TOKEN looks like a JWT ({len(token)} chars)")
        expiry = token_expiry(token)
        if expiry is None:
            warn("token carries no readable `exp` claim; expiry cannot be checked")
        else:
            days = (expiry - datetime.now(timezone.utc)).days
            when = expiry.date().isoformat()
            if days < 0:
                bad(f"token EXPIRED on {when}. Issue a new one.")
            elif days <= TOKEN_WARN_DAYS:
                warn(f"token expires in {days} days ({when}) — issue a new one soon")
            else:
                ok(f"token valid for {days} more days (expires {when})")

    sync_staleness()

    drift = alias_drift()
    if drift:
        warn(f"payees.md and payee_aliases.yaml disagree on {len(drift)} token(s):")
        for line in drift[:10]:
            console.print(f"          {line}")
        console.print(
            "        [dim]the yaml is what the code reads; payees.md is a note to "
            "yourself. Nothing is synced automatically.[/dim]"
        )
    elif load_payee_aliases():
        ok("payees.md agrees with payee_aliases.yaml")

    console.print("\n[bold]firefly[/bold]")
    if problems:
        console.print("  [dim]skipped — fix the configuration above first[/dim]")
        raise typer.Exit(1)

    try:
        with FireflyClient(settings.firefly_url, token) as client:
            about = client.about()
            ok(f"reachable at {settings.firefly_url} (v{about.get('version')}, "
               f"api v{about.get('api_version')}, {about.get('driver')})")

            accounts = client.asset_accounts()
            names = [a["attributes"]["name"] for a in accounts]
            if not accounts:
                bad("no asset accounts exist — create one in Firefly first")
            else:
                ok(f"{len(accounts)} asset account(s): {', '.join(repr(n) for n in names)}")

            configured = settings.passbook_asset_account
            if not configured:
                bad(
                    "PASSBOOK_ASSET_ACCOUNT is not set in .env — refusing to guess "
                    f"which of {len(accounts)} account(s) to post into"
                )
            elif configured not in names:
                bad(f"PASSBOOK_ASSET_ACCOUNT {configured!r} is not one of {names}")
            else:
                match = next(a for a in accounts if a["attributes"]["name"] == configured)
                currency = match["attributes"].get("currency_code")
                ok(f"target account {configured!r} exists (currency {currency})")
                if currency != "INR":
                    bad(f"target account currency is {currency}, expected INR")
    except FireflyError as exc:
        bad(str(exc))

    # --- the live ledger itself. SPEC §20, and the gap §19 exposed: everything
    #     above can pass while Firefly holds a third of the ledger.
    registry = load_accounts(settings=settings)
    if settings.firefly_token and registry:
        trashed = _trashed_journals()
        intents = [p.name for p in ops.outstanding_purge_intents()]
        try:
            with FireflyClient(settings.firefly_url, settings.firefly_token) as client:
                for entry in registry:
                    console.print(
                        f"\n[bold]ledger integrity[/bold] — {entry.slug} ({entry.masked})"
                    )
                    _print_verdict(
                        service.verify_ledger(
                            client, entry, trashed=trashed, intents=intents
                        ),
                        ok=ok, warn=warn, bad=bad,
                    )
        except FireflyError as exc:
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
    if not settings.firefly_token:
        err.print("[red]FIREFLY_TOKEN is not set.[/red] Run `passbook doctor`.")
        raise typer.Exit(5)
    try:
        with FireflyClient(settings.firefly_url, settings.firefly_token) as client:
            account = service.resolve_account(meta, settings, client=client)
    except UnknownAccount as exc:
        err.print(f"[red]unregistered account:[/red] {exc}")
        err.print("Add it with: [bold]passbook accounts add[/bold]")
        raise typer.Exit(4) from exc
    except AccountMismatch as exc:
        err.print(f"[red]account assertion failed:[/red] {exc}")
        raise typer.Exit(4) from exc
    if not account.asset_account:
        err.print(
            "[red]no Firefly asset account for this account.[/red] "
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


def _resume_purge(settings) -> None:
    """Finish an interrupted purge/re-push cycle. SPEC §19.7.

    The intent file says what was being deleted and what has to go back. The
    LEDGER, not the file, is the source of truth for what remains — so this
    re-derives the candidates rather than trusting a `deleted` list that was
    itself written by the run that died.
    """
    outstanding = ops.outstanding_purge_intents()
    if not outstanding:
        console.print("[green]nothing to resume[/green] — no unfinished purge recorded.")
        return
    if len(outstanding) > 1:
        console.print(
            f"[yellow]{len(outstanding)} unfinished purges recorded[/yellow]; "
            "finishing the oldest first."
        )

    path = outstanding[0]
    intent = ops.read_purge_intent(path)
    target = intent.get("account") or settings.passbook_asset_account
    console.print(
        f"[bold]resuming[/bold] {path.name}\n"
        f"  recorded           {intent.get('created')}\n"
        f"  stage              {intent.get('stage')}\n"
        f"  account            {target!r}\n"
        f"  rows to restore    {intent.get('expected_rows')}\n"
        f"  statements         {len(intent.get('statements') or [])}\n"
    )
    if not settings.firefly_token:
        err.print("[red]FIREFLY_TOKEN is not set.[/red]")
        raise typer.Exit(5)

    with FireflyClient(settings.firefly_url, settings.firefly_token) as client:
        accounts = {a["attributes"]["name"]: a["id"] for a in client.asset_accounts()}
        if target not in accounts:
            err.print(f"[red]no asset account named {target!r}[/red]")
            raise typer.Exit(5)

        # 1. finish the delete, if it never got through the whole list.
        recorded = set(intent.get("external_ids") or [])
        candidates, _ = find_candidates(client, accounts[target])
        left = [c for c in candidates if c.external_id in recorded]
        if left:
            console.print(f"  {len(left)} recorded row(s) still present — deleting")
            result = purge_transactions(client, left, intent=path)
            if not result.ok:
                err.print(f"[red]{result.failed} delete(s) failed; nothing pushed.[/red]")
                raise typer.Exit(6)
        else:
            console.print("  delete stage already complete")
            ops.update_purge_intent(path, stage="purged")

        # 2. push the statements back. Duplicates are the normal outcome here —
        #    a resume of a run that got partway through re-pushing will hit them.
        ops.update_purge_intent(path, stage="repushing")
        pushed = duplicates = failed = 0
        for name in intent.get("statements") or []:
            statement = Path(name)
            if not statement.is_file():
                console.print(f"  [yellow]missing[/yellow] {name} — cannot re-push it")
                failed += 1
                continue
            parsed = service.parse_statement(statement)
            service.account_matches(parsed.meta, settings)
            outcome = service.push_statement(parsed, settings, client)
            pushed += outcome.pushed
            duplicates += outcome.duplicates
            failed += outcome.failed
            console.print(
                f"  {statement.name:<40} pushed {outcome.pushed:>3}  "
                f"duplicates {outcome.duplicates:>3}  failed {outcome.failed}"
            )

        # 3. only clear the intent once the LEDGER says it is whole.
        verdict = service.verify_ledger(
            client, settings, trashed=_trashed_journals(), intents=[]
        )

    console.print(f"\npushed {pushed}, duplicates {duplicates}, failed {failed}\n")
    console.print("[bold]ledger integrity[/bold]")
    _print_verdict(
        verdict,
        ok=lambda m: console.print(f"  [green]ok[/green]    {m}"),
        warn=lambda m: console.print(f"  [yellow]warn[/yellow]  {m}"),
        bad=lambda m: console.print(f"  [red]FAIL[/red]  {m}"),
    )
    if verdict.failed or failed:
        console.print(
            f"\n[red bold]not clearing {path.name}[/red bold] — the cycle is still "
            "unfinished, and the record is what makes that visible."
        )
        raise typer.Exit(7)
    ops.clear_purge_intent(path)
    console.print(f"\n[green]resumed and verified;[/green] {path.name} cleared.")


# --- ledger integrity --------------------------------------------------------


def _trashed_journals() -> int | None:
    """Count soft-deleted journals, or None if it cannot be asked.

    **Deliberately here and not in `ops.py`.** Firefly's API cannot answer this —
    verified against the pinned tag, `routes/api.php` exposes only
    `DELETE data/destroy` and `DELETE data/purge`, neither of which lists trashed
    journals — so it needs the database, which means `docker compose exec`. That
    ability must never reach the web container (§15.1), and
    `test_ops_only_ever_executes_rclone` enforces it at AST level. The CLI runs on
    the host, where the socket is already the operator's.

    None is returned for *any* obstacle, and `verify_ledger` then reports the
    check as unchecked rather than passed.
    """
    import subprocess

    settings = load_settings()
    env = {}
    for line in (Path(".env").read_text().splitlines() if Path(".env").is_file() else []):
        if line.startswith(("DB_USERNAME=", "DB_DATABASE=", "DB_PASSWORD=")):
            key, _, value = line.partition("=")
            env[key] = value.strip().strip("\"'")
    if not {"DB_USERNAME", "DB_DATABASE", "DB_PASSWORD"} <= env.keys():
        return None
    try:
        done = subprocess.run(
            [
                "docker", "compose", "exec", "-T",
                "-e", f"PGPASSWORD={env['DB_PASSWORD']}",
                "db", "psql", "-qtAX", "-U", env["DB_USERNAME"], "-d", env["DB_DATABASE"],
                "-c", "select count(*) from transaction_journals where deleted_at is not null;",
            ],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if done.returncode != 0:
        return None
    try:
        return int(done.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return None
    _ = settings  # settings are read for .env discovery only


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
    """Check the LIVE ledger against the statements that built it. SPEC §20.

    The gap the 2026-08-11 incident exposed (§19): the continuity invariant
    (§6.6) validates a file at parse time, and nothing validated Firefly. A purge
    plus an interrupted re-push left 21 of 93 rows with a self-consistent balance,
    and every existing check passed for seven hours.

    Exits non-zero if any check fails, so it can gate a script.
    """
    _setup_logging(verbose)
    settings = load_settings()
    if account:
        settings = settings.model_copy(update={"passbook_asset_account": account})
    if not settings.firefly_token or not settings.passbook_asset_account:
        err.print("[red]FIREFLY_TOKEN or PASSBOOK_ASSET_ACCOUNT is not set.[/red] Run `passbook doctor`.")
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
    trashed = _trashed_journals()
    intents = [p.name for p in ops.outstanding_purge_intents()]
    try:
        with FireflyClient(settings.firefly_url, settings.firefly_token) as client:
            for entry in registry:
                verdicts.append(
                    (entry, service.verify_ledger(client, entry, trashed=trashed, intents=intents))
                )
    except FireflyError as exc:
        err.print(f"[red]Firefly did not answer:[/red] {exc}")
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
    """Push one statement into Firefly. SPEC §7.2."""
    _setup_logging(verbose)
    meta, transactions, warnings = _read(file)
    settings, account = _require_pushable(meta)

    if dry_run:
        console.print(
            f"[bold]dry run[/bold] — {len(transactions)} payloads for "
            f"{meta.masked_account}, routed to {account.slug} "
            f"({account.asset_account!r}). Nothing is posted.\n"
        )
        for txn in transactions[:3]:
            console.print_json(
                json.dumps(
                    build_payload(txn, account)
                )
            )
        if len(transactions) > 3:
            console.print(f"[dim]... and {len(transactions) - 3} more[/dim]")
        kinds = defaultdict(int)
        for txn in transactions:
            kinds[
                build_payload(txn, account)["transactions"][0]["type"]
            ] += 1
        console.print(f"\ntypes: {dict(kinds)}")
        console.print(f"rows parsed        {len(transactions)}\nwould push         {len(transactions)}")
        return

    with FireflyClient(settings.firefly_url, settings.firefly_token) as client:
        with console.status(f"pushing {len(transactions)} transactions..."):
            result = push_transactions(client, transactions, account)
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

        with FireflyClient(settings.firefly_url, settings.firefly_token) as client:
            with console.status(f"pushing {len(transactions)}..."):
                result = push_transactions(client, transactions, account)
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
def bootstrap(
    dry_run: bool = typer.Option(False, "--dry-run", help="show what would be created"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Create Firefly rules from config/rules.yaml. Idempotent. SPEC §8."""
    _setup_logging(verbose)
    settings = load_settings()
    if not settings.firefly_token:
        err.print("[red]FIREFLY_TOKEN is not set.[/red] Run `passbook doctor`.")
        raise typer.Exit(5)

    config = load_rules()
    if not config:
        err.print(f"[red]no rules found[/red] — {RULES_FILE} is missing or empty.")
        raise typer.Exit(1)

    with FireflyClient(settings.firefly_url, settings.firefly_token) as client:
        result = bootstrap_rules(
            client, config, settings.large_txn_threshold, dry_run=dry_run
        )

    verb = "would create" if dry_run else "created"
    console.print(f"{verb:<14} {len(result.created)}")
    for title in result.created:
        console.print(f"  [green]+[/green] {title}")
    if result.updated:
        console.print(f"updated        {len(result.updated)}")
        for title in result.updated:
            console.print(f"  [yellow]~[/yellow] {title}")
    console.print(f"already present {len(result.existing)}")
    for title in result.existing:
        console.print(f"  [dim]=[/dim] {title}")
    if result.failed:
        console.print(f"[red]failed         {len(result.failed)}[/red]")
        for title, message in result.failed:
            console.print(f"  [red]![/red] {title}: {message}")

    bills = load_bills()
    console.print(
        f"\nbills           {len(bills)}"
        + ("" if bills else "  [dim](bills.yaml is empty by design — SPEC §8)[/dim]")
    )
    if not result.ok:
        raise typer.Exit(6)


@app.command()
def purge(
    account: str = typer.Option(None, help="asset account name; defaults to PASSBOOK_ASSET_ACCOUNT"),
    confirm: bool = typer.Option(False, "--confirm", help="actually delete; omit for a dry run"),
    yes: bool = typer.Option(False, "--yes", help="skip the interactive prompt"),
    resume: bool = typer.Option(False, "--resume", help="finish an interrupted purge (SPEC §19.7)"),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Delete transactions passbook pushed into an asset account.

    Dry run unless --confirm. Only groups carrying an `external_id` are touched,
    so an opening balance is excluded structurally rather than by a date guard.

    Use when a re-push is needed — aliases and rules apply at push time, and
    re-pushing over existing rows just hits dedup.

    **Intent is recorded before the first delete** (§19.7), so an interrupted run
    is detectable by `verify-ledger` and completable with `--resume`.
    """
    _setup_logging(verbose)
    settings = load_settings()
    if resume:
        _resume_purge(settings)
        return
    target = account or settings.passbook_asset_account
    if not target:
        err.print("[red]no account given[/red] and PASSBOOK_ASSET_ACCOUNT is unset.")
        raise typer.Exit(5)
    if not settings.firefly_token:
        err.print("[red]FIREFLY_TOKEN is not set.[/red] Run `passbook doctor`.")
        raise typer.Exit(5)

    with FireflyClient(settings.firefly_url, settings.firefly_token) as client:
        accounts = {a["attributes"]["name"]: a["id"] for a in client.asset_accounts()}
        if target not in accounts:
            err.print(f"[red]no asset account named {target!r}[/red]; have {list(accounts)}")
            raise typer.Exit(5)

        candidates, protected = find_candidates(client, accounts[target])

        if not candidates:
            console.print(f"nothing to purge on {target!r} ({len(protected)} protected).")
            return

        total = sum(c.amount for c in candidates)
        dates = sorted(c.date for c in candidates)
        console.print(
            f"[bold]{'PURGE' if confirm else 'dry run'}[/bold] on {target!r} "
            f"(account id {accounts[target]})\n"
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

        statements = [str(p) for p in _archived_statements_paths()]
        if not statements:
            console.print(
                "[yellow]note[/yellow] archive/ is empty, so the recorded intent "
                "has nothing to re-push; a resume will only finish the delete."
            )
        with console.status(f"deleting {len(candidates)}..."):
            result = purge_transactions(
                client, candidates, account=target, statements=statements
            )

    console.print(
        f"\ndeleted            {result.deleted}\n"
        f"already gone       {result.already_gone}\n"
        f"failed             {result.failed}\n"
        f"trashed purged     {'yes' if result.hard_purged else 'no'}"
        + ("" if result.hard_purged else "  [yellow](re-push may hit dedup)[/yellow]")
    )
    for external_id, message in result.failures[:10]:
        console.print(f"  [red]fail[/red] {external_id}: {message}")
    if result.intent:
        console.print(
            f"\nintent recorded    {result.intent.name}\n"
            "[yellow]The ledger is now short until the statements are pushed back.[/yellow] "
            "Run `passbook purge --resume` (or `passbook sync`) to finish; "
            "`passbook verify-ledger` reports the gap until then."
        )
    if not result.ok:
        raise typer.Exit(6)


# --- migrations --------------------------------------------------------------


def _migration_context(client, settings, registry):
    """Everything a migration is allowed to touch. SPEC §22.2.

    The dangerous half is supplied as callables so a migration cannot grow its
    own copy of the purge path — `purge_and_repush` is the same code
    `passbook purge --confirm --yes` and `--resume` run.
    """
    from . import migrate

    def purge_and_repush(account) -> None:
        accounts = {a["attributes"]["name"]: a["id"] for a in client.asset_accounts()}
        if account.asset_account not in accounts:
            raise RuntimeError(f"no Firefly asset account named {account.asset_account!r}")
        candidates, _ = find_candidates(client, accounts[account.asset_account])
        if not candidates:
            console.print(f"  {account.slug}: nothing pushed yet, nothing to migrate")
            return
        statements = [str(p) for p in _archived_statements_paths()]
        if not statements:
            # Deleting rows this machine cannot rebuild is not a migration; it is
            # data loss with a progress bar.
            raise RuntimeError(
                f"{account.slug}: {len(candidates)} row(s) in Firefly but nothing in "
                "archive/ to push back. Re-download the statements first."
            )
        console.print(f"  {account.slug}: purging {len(candidates)} row(s)")
        result = purge_transactions(
            client,
            candidates,
            account=account.asset_account,
            statements=statements,
            slug=account.slug,
        )
        if not result.ok:
            raise RuntimeError(f"{account.slug}: {result.failed} delete(s) failed")
        console.print(f"  {account.slug}: re-pushing {len(statements)} statement(s)")
        _resume_purge(settings)

    return migrate.Context(
        settings=settings,
        client=client,
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

    if not settings.firefly_token or not settings.passbook_asset_account:
        err.print(
            "[red]FIREFLY_TOKEN or PASSBOOK_ASSET_ACCOUNT is not set.[/red] "
            "Migrations read the live ledger to decide what is pending, so there "
            "is nothing they can honestly say without it. Run `passbook doctor`."
        )
        raise typer.Exit(5)

    registry = load_accounts(settings=settings)
    with FireflyClient(settings.firefly_url, settings.firefly_token) as client:
        ctx = _migration_context(client, settings, registry)
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
                    f"Nothing recorded. Recover from {newest.name} if the ledger is "
                    "short — `passbook verify-ledger` will say which it is."
                )
                raise typer.Exit(7)
            console.print("  [green]verified[/green]")

        verdict = service.LedgerVerdict(
            [
                item
                for entry in registry
                for item in service.verify_ledger(
                    client,
                    entry,
                    trashed=_trashed_journals(),
                    intents=[p.name for p in ops.outstanding_purge_intents()],
                ).checks
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
