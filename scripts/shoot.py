#!/usr/bin/env python3
"""Screenshot every page, both themes, desktop and mobile.

Phase 10 was verified through the API and the test suite and never actually
looked at. This exists so that cannot happen again.

Runs the real app against the real Firefly and the real archive, with a
throwaway credential injected in memory — `config/web-auth.json` is never read
or written, so the operator's own password and TOTP enrolment are untouched.
Uploads go to a temporary inbox and are discarded.

    uv run python scripts/shoot.py before
    uv run python scripts/shoot.py after
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
import threading
from pathlib import Path

import pyotp
from playwright.sync_api import sync_playwright
from waitress import serve

ROOT = Path(__file__).resolve().parent.parent
# Anchored on this file, never on the working directory. `shoot_written` and
# `demo_ledger.py --shoot` both run this from a scratch directory, and a
# relative path to a repo file resolves inside that scratch dir — where it does
# not exist.
FIXTURE = ROOT / "tests" / "fixtures" / "statement.xls"

sys.path.insert(0, str(ROOT / "src"))

from passbook import webauth  # noqa: E402
from passbook.web import create_app  # noqa: E402

CHROME = os.environ.get(
    "PW_CHROME",
    str(Path.home() / ".cache/ms-playwright/chromium-1228/chrome-linux64/chrome"),
)
USER = "shots"
PASSWORD = "shots-shots-shots"
SECRET = pyotp.random_base32()

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}

# (route, label, needs_pending)
#
# Three of these are no longer nav items (Phase 13 cut six to three), and they
# are still shot: Re-apply is reached from Payees, Status from the strip on the
# Ledger, Account from the header menu. A page that is one click further away is
# not a page that stopped needing to be looked at.
PAGES = [
    ("/", "ledger", False),
    ("/upload", "upload", False),
    ("/preview", "preview", True),
    ("/payees", "payees", False),
    ("/reapply", "reapply", False),
    ("/status", "status", False),
    ("/password", "account", False),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(auth: webauth.WebAuth, inbox: Path) -> int:
    port = free_port()
    app = create_app(
        {
            "WEB_AUTH_FIXED": auth,
            "SECRET_KEY": "screenshots",
            "INBOX": inbox,
            "ARCHIVE": Path("archive"),
        }
    )
    threading.Thread(
        target=lambda: serve(app, host="127.0.0.1", port=port, threads=4, _quiet=True),
        daemon=True,
    ).start()
    return port


def sign_in(page, base: str, auth: webauth.WebAuth) -> None:
    """Sign in for real, through the actual two-factor flow.

    The counter is cleared first because this harness signs in once per
    theme/viewport combination, and four sign-ins inside one 30-second window
    reuse the same TOTP code — which the replay guard correctly refuses. That
    is the guard working, not a bug to route around in production.
    """
    auth.totp_last_counter = None
    page.goto(f"{base}/", wait_until="domcontentloaded")
    page.fill("#username", USER)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_selector("#code", timeout=15000)
    page.fill("#code", pyotp.TOTP(SECRET).now())
    page.click("button[type=submit]")
    page.wait_for_selector("nav", timeout=15000)


def stage_statement(page, base: str, statement: Path) -> None:
    """Upload a real statement so Preview has real rows. Never pushed."""
    page.goto(f"{base}/upload", wait_until="domcontentloaded")
    page.set_input_files("#statement", str(statement))
    page.click("button[type=submit]")
    page.wait_for_url("**/preview", timeout=60000)


def shoot_menu(page, base: str, out: Path, theme: str, size: str, written: list[str]) -> None:
    """The header menu, open.

    Account, Status and Sign out moved in here, so a closed `<details>` is the
    only state a page shot can ever capture — and the one state that shows none
    of them. Viewport rather than full_page: the panel is pinned to the top and a
    full-page shot of a long ledger reduces it to a smudge.
    """
    page.goto(f"{base}/", wait_until="domcontentloaded")
    try:
        # Wait for the charts first, or the menu is photographed over a page of
        # skeletons — which is a picture of the loading state, not of the menu.
        page.wait_for_selector(".bars__fill", timeout=25000)
        page.click(".menu > summary", timeout=8000)
        page.wait_for_selector(".menu__panel", timeout=5000)
        page.wait_for_timeout(250)
    except Exception:
        return
    name = f"menu-{theme}-{size}.png"
    page.screenshot(path=out / name)
    written.append(name)


def shoot_payee_diff(page, base: str, out: Path, theme: str, size: str, written: list[str]) -> None:
    """Payees -> Review changes, with a real pending edit.

    `/api/payees/diff` plans and writes nothing (§14.4), so this is safe against
    the operator's own config — the alias typed here never reaches a file. The
    reconcile card that follows a *write* is the same `ReconcileCall` component
    the `/reapply` shot already covers.
    """
    page.goto(f"{base}/payees", wait_until="domcontentloaded")
    try:
        page.wait_for_selector("table.payees input", timeout=20000)
        page.fill("table.payees tbody tr:first-child input", "Shots Alias")
        page.click("button[type=submit]")
        page.wait_for_url("**/payees/diff", timeout=30000)
        page.wait_for_selector(".diff", timeout=15000)
        page.wait_for_timeout(400)
    except Exception:
        return
    name = f"payees-diff-{theme}-{size}.png"
    page.screenshot(path=out / name, full_page=True)
    written.append(name)


def shoot_written(browser, base: str, auth, out: Path, written: list[str]) -> None:
    """Payees -> Review changes -> **Written**, the page that follows a real write.

    This is the state the phase exists for: config is on disk and the only
    question left is whether to reconcile the rows already in Firefly. It cannot
    be reached without writing config, and writing the operator's own
    `config/payee_aliases.yaml` to take a screenshot is not acceptable.

    So the process runs in a scratch directory holding a *copy* of `config/` and
    symlinks to `.env`, `archive/` and `backups/`. Everything this app reads by
    relative path — settings, aliases, rules, statements, the dump freshness —
    resolves inside the scratch dir, and the write lands on the copy. The CWD is
    restored and the scratch dir deleted in a `finally`.

    Two things it does touch, deliberately and idempotently: `/payees/apply`
    syncs rules to Firefly, and since the copied config is identical to the real
    one that is a no-op ("N unchanged"); and `/reapply` then reports a genuine
    count, because the one alias typed here really would rename rows. The
    destructive button is never clicked.
    """
    import os

    scratch = Path(tempfile.mkdtemp(prefix="shots-config-"))
    shutil.copytree("config", scratch / "config")
    for name in (".env", "archive", "backups"):
        source = Path(name).resolve()
        if source.exists():
            (scratch / name).symlink_to(source)
    origin = Path.cwd()
    assert (scratch / "config").resolve() != (origin / "config").resolve()

    try:
        os.chdir(scratch)
        for theme in ("light", "dark"):
            ctx = browser.new_context(viewport=DESKTOP, color_scheme=theme, device_scale_factor=2)
            page = ctx.new_page()
            sign_in(page, base, auth)
            page.goto(f"{base}/payees", wait_until="domcontentloaded")
            try:
                page.wait_for_selector("table.payees input", timeout=25000)
                page.fill("table.payees tbody tr:first-child input", f"Written {theme}")
                page.click("button[type=submit]")
                page.wait_for_url("**/payees/diff", timeout=30000)
                page.click("text=Write config and sync rules")
                # The h1 changes to "Config written"; then the reconcile card
                # resolves from its skeleton into a count.
                page.wait_for_selector("h1:has-text('Config written')", timeout=60000)
                page.wait_for_function(
                    "!document.querySelector('.skel')", timeout=60000
                )
                page.wait_for_timeout(500)
            except Exception as exc:
                print(f"  written-{theme}: skipped ({type(exc).__name__})")
                ctx.close()
                continue
            name = f"written-{theme}-desktop.png"
            page.screenshot(path=out / name, full_page=True)
            written.append(name)
            ctx.close()
    finally:
        os.chdir(origin)
        shutil.rmtree(scratch, ignore_errors=True)



def shoot_accounts(browser, base: str, auth, out: Path, written: list[str]) -> None:
    """The two-account state: switcher, scoped Ledger, and All accounts. §21.9.

    The live install has ONE account, which is the shape almost every user has —
    and the single-account shots elsewhere in this run are the evidence that no
    switcher appears there. To photograph the other shape without registering
    anything against the operator's own config, this runs in a scratch directory
    holding a **copy** of `config/` and of `archive/`, with a two-account registry
    written into the copy.

    The second account points at an existing, EMPTY Firefly asset account, so it
    has no pushed rows: what these shots demonstrate is the switcher, the scoping,
    and the summed balance with its parts. Nothing is pushed anywhere.
    """
    import os

    from passbook.config import Account, load_accounts, save_accounts

    real = load_accounts()
    if not real:
        print("  accounts: skipped (nothing registered)")
        return

    scratch = Path(tempfile.mkdtemp(prefix="shots-accounts-"))
    shutil.copytree("config", scratch / "config")
    shutil.copytree("archive", scratch / "archive")
    for name in (".env", "backups"):
        source = Path(name).resolve()
        if source.exists():
            (scratch / name).symlink_to(source)

    # A second registry entry, pointing at an asset account Firefly already has
    # and passbook has never pushed into.
    second = Account(
        slug="canara-cash",
        bank="canara",
        account_number="000000009999",
        asset_account="Cash wallet",
        label="Cash wallet",
    )
    origin = Path.cwd()
    try:
        os.chdir(scratch)
        save_accounts([*real, second])
        for theme in ("light", "dark"):
            for size, device in (("desktop", DESKTOP), ("mobile", MOBILE)):
                for selection, tag in ((real[0].slug, "one"), ("all", "all")):
                    ctx = browser.new_context(
                        viewport=device, color_scheme=theme, device_scale_factor=2
                    )
                    # The selection lives in localStorage (§21.9) — set it before
                    # the app boots so the first render is already scoped.
                    ctx.add_init_script(
                        f"try {{ localStorage.setItem('passbook.account', '{selection}') }} "
                        "catch (e) {}"
                    )
                    page = ctx.new_page()
                    try:
                        sign_in(page, base, auth)
                        page.goto(f"{base}/", wait_until="domcontentloaded")
                        page.wait_for_selector(".switcher select", timeout=25000)
                        page.wait_for_function(
                            "!document.querySelector('.skel')", timeout=40000
                        )
                        page.wait_for_timeout(400)
                    except Exception as exc:
                        print(f"  accounts-{tag}-{theme}-{size}: skipped ({type(exc).__name__})")
                        ctx.close()
                        continue
                    name = f"accounts-{tag}-{theme}-{size}.png"
                    page.screenshot(path=out / name, full_page=(size == "desktop"))
                    written.append(name)
                    ctx.close()
    finally:
        os.chdir(origin)
        shutil.rmtree(scratch, ignore_errors=True)


def shoot_drift(browser, base: str, auth, out: Path, written: list[str]) -> None:
    """The state where something needs doing — which the live ledger is not in.

    Re-apply exists for when the ledger disagrees with config, and the whole
    point of Phase 13 was surfacing that. But a reconciled ledger reports zero
    changes, so the prompt, its count and its danger button are exactly the
    surfaces a shoot against real data can never show. The preview is therefore
    faked here — and **only** here, in the harness, never in the app.

    The rows come from `tests/fixtures/statement.xls` through the parser (§16.6:
    nothing displayed anywhere is hand-typed), with the descriptions altered so
    they read as renames.
    """
    from passbook import ops, service
    from passbook.loaders import xls

    _, transactions = xls.load(FIXTURE)
    changes = [
        service.ReapplyChange(
            external_id=txn.txn_id,
            date=txn.txn_date.isoformat(),
            amount=(txn.debit or txn.credit or 0),
            old_description=f"{txn.payee or 'Unknown'} ({txn.channel})",
            new_description=f"Canteen ({txn.channel})",
            old_category="",
            new_category="Morning Stall",
        )
        for txn in transactions[:6]
    ]
    original = service.reapply_preview
    service.reapply_preview = lambda *a, **k: (changes, len(transactions))
    try:
        for theme in ("light", "dark"):
            ctx = browser.new_context(viewport=DESKTOP, color_scheme=theme, device_scale_factor=2)
            page = ctx.new_page()
            sign_in(page, base, auth)
            for route, label, selector in (
                ("/reapply", "drift-reapply", ".actions button.danger"),
                ("/payees", "drift-payees", ".notice--warn"),
            ):
                page.goto(f"{base}{route}", wait_until="domcontentloaded")
                try:
                    page.wait_for_selector(selector, timeout=30000)
                    page.wait_for_timeout(400)
                except Exception:
                    continue
                name = f"{label}-{theme}-desktop.png"
                page.screenshot(path=out / name, full_page=True)
                written.append(name)
            ctx.close()
            # ...and the refusal, which is the state that matters most and the
            # one a healthy machine never produces: with a fresh dump on disk the
            # purge is allowed, so the only way to photograph the guard is to
            # move the goalposts rather than to falsify a real backup's
            # timestamp. Zero minutes makes every dump stale.
            # The Ledger strip's failing state. A healthy ledger cannot produce
            # it, and it is the item that would have caught §19's incident — so
            # the verdict is faked here, in the harness, for exactly two shots.
            real_verify = service.verify_ledger

            def short_ledger(client, settings, *a, **k):
                verdict = real_verify(client, settings, *a, **k)
                broken = [
                    service.Check(
                        "rows",
                        False,
                        "21 live vs 93 archived — 72 archived row(s) MISSING from "
                        "Firefly (20260516000001, 20260516000002, 20260517000001, "
                        "20260520000001, 20260520000002 …)",
                    )
                ] + [c for c in verdict.checks if c.name != "rows"]
                return service.LedgerVerdict(broken)

            service.verify_ledger = short_ledger
            try:
                ctx = browser.new_context(
                    viewport=DESKTOP, color_scheme=theme, device_scale_factor=2
                )
                page = ctx.new_page()
                sign_in(page, base, auth)
                page.goto(f"{base}/", wait_until="domcontentloaded")
                page.wait_for_selector(".strip__item.bad", timeout=30000)
                page.wait_for_timeout(300)
                name = f"strip-broken-{theme}-desktop.png"
                page.screenshot(path=out / name, clip={"x": 0, "y": 0, "width": 1440, "height": 460})
                written.append(name)
                ctx.close()
            except Exception as exc:
                print(f"  strip-broken-{theme}: skipped ({type(exc).__name__})")
            finally:
                service.verify_ledger = real_verify

            allowed = ops.REAPPLY_DUMP_MAX_AGE_MINUTES
            ops.REAPPLY_DUMP_MAX_AGE_MINUTES = 0
            try:
                ctx = browser.new_context(
                    viewport=DESKTOP, color_scheme=theme, device_scale_factor=2
                )
                page = ctx.new_page()
                sign_in(page, base, auth)
                page.goto(f"{base}/reapply", wait_until="domcontentloaded")
                page.wait_for_selector(".actions button.danger[disabled]", timeout=30000)
                page.wait_for_timeout(300)
                name = f"drift-nodump-{theme}-desktop.png"
                page.screenshot(path=out / name, full_page=True)
                written.append(name)
                ctx.close()
            except Exception as exc:
                print(f"  drift-nodump-{theme}: skipped ({type(exc).__name__})")
            finally:
                ops.REAPPLY_DUMP_MAX_AGE_MINUTES = allowed
    finally:
        service.reapply_preview = original


def shoot(tag: str) -> None:
    # Absolute, not relative: `shoot_written` changes the working directory, and
    # a relative output path would resolve inside its scratch dir — which is
    # exactly where the first attempt wrote a shot and then deleted it.
    out = (Path("docs/shots") / tag).resolve()
    out.mkdir(parents=True, exist_ok=True)
    inbox = Path(tempfile.mkdtemp(prefix="shots-inbox-"))

    enrolled = webauth.WebAuth(
        username=USER,
        password_hash=webauth.hash_password(PASSWORD),
        totp_secret=SECRET,
        totp_enrolled_at="2026-08-10T00:00:00+00:00",
        salt="0" * 32,
    )
    port = start(enrolled, inbox)
    base = f"http://127.0.0.1:{port}"

    # A statement to drive Upload -> Preview with. `archive/` on a working
    # install; the committed fixture otherwise — which is every fresh clone, and
    # every screenshot that goes INTO this repository (§22.1). Falling back
    # rather than dying is what lets `demo_ledger.py --shoot` work at all.
    archived = sorted(Path("archive").rglob("*.xls"))
    statement = archived[0] if archived else FIXTURE
    if not archived:
        print(f"  no archive/ — driving Upload with {statement.name} (the fixture)")
    written: list[str] = []

    with sync_playwright() as p:
        # --disable-dev-shm-usage: WSL2's /dev/shm is small and chromium
        # crashes rendering the 13,000px full-page shots without it.
        browser = p.chromium.launch(
            executable_path=CHROME,
            args=["--disable-dev-shm-usage", "--disable-gpu", "--no-sandbox"],
        )
        for theme in ("light", "dark"):
            for size, device in (("desktop", DESKTOP), ("mobile", MOBILE)):
                ctx = browser.new_context(
                    viewport=device, color_scheme=theme, device_scale_factor=2
                )
                page = ctx.new_page()

                # Sign-in and enrolment, before a session exists.
                page.goto(f"{base}/", wait_until="domcontentloaded")
                name = f"signin-{theme}-{size}.png"
                page.screenshot(path=out / name, full_page=True)
                written.append(name)

                sign_in(page, base, enrolled)
                stage_statement(page, base, statement)

                for route, label, _ in PAGES:
                    page.goto(f"{base}{route}", wait_until="domcontentloaded")
                    # Wait for the page's own content, not for the network to
                    # go quiet — `networkidle` is flaky under memory pressure
                    # and was timing out at 30s on a page that had rendered.
                    try:
                        page.wait_for_selector("h1", timeout=20000)
                        page.wait_for_function(
                            "!document.querySelector('.spinner')", timeout=20000
                        )
                        # And no SKELETON either. Phase 13 gave the Ledger a
                        # second query with its own skeleton, and this loop only
                        # waited on `.spinner` — so the shot was a race that
                        # happened to be won until the charts got slower, and
                        # then silently produced a full-page picture of five
                        # loading placeholders. A screenshot of the wrong state
                        # is worse than no screenshot: it looks fine.
                        page.wait_for_function(
                            "!document.querySelector('.skel')", timeout=30000
                        )
                    except Exception:
                        pass
                    page.wait_for_timeout(700)
                    name = f"{label}-{theme}-{size}.png"
                    page.screenshot(path=out / name, full_page=True)
                    written.append(name)

                # Two states that are not routes. Both were invisible to this
                # harness until Phase 13 put things in them.
                shoot_menu(page, base, out, theme, size, written)
                shoot_payee_diff(page, base, out, theme, size, written)

                # Discard the staged file so nothing is left behind.
                page.goto(f"{base}/preview", wait_until="domcontentloaded")
                try:
                    page.click("text=Discard", timeout=4000)
                    page.wait_for_timeout(500)
                except Exception:
                    pass
                ctx.close()

        shoot_written(browser, base, enrolled, out, written)
        shoot_accounts(browser, base, enrolled, out, written)
        shoot_drift(browser, base, enrolled, out, written)

        # Enrolment needs a credential with no secret yet.
        fresh = webauth.WebAuth(
            username=USER, password_hash=webauth.hash_password(PASSWORD), salt="0" * 32
        )
        port2 = start(fresh, inbox)
        base2 = f"http://127.0.0.1:{port2}"
        for theme in ("light", "dark"):
            ctx = browser.new_context(viewport=DESKTOP, color_scheme=theme, device_scale_factor=2)
            page = ctx.new_page()
            page.goto(f"{base2}/", wait_until="domcontentloaded")
            page.fill("#username", USER)
            page.fill("#password", PASSWORD)
            page.click("button[type=submit]")
            page.wait_for_selector(".qr, #code", timeout=15000)
            page.wait_for_timeout(600)
            name = f"enrol-{theme}-desktop.png"
            page.screenshot(path=out / name, full_page=True)
            written.append(name)
            ctx.close()
        browser.close()

    shutil.rmtree(inbox, ignore_errors=True)
    print(f"{len(written)} shots -> {out}")
    for name in sorted(set(written)):
        size_kb = (out / name).stat().st_size // 1024
        print(f"  {name:34} {size_kb:>4} KB")


if __name__ == "__main__":
    shoot(sys.argv[1] if len(sys.argv) > 1 else "before")
