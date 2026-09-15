#!/usr/bin/env python3
"""Shoot the reconcile screen with rows on it. SPEC §24.

The empty state of `/reapply` is easy to photograph and says nothing about the
feature: the whole of §24 is what the page does when rows **do** differ. That
state needs a ledger with rows already in it, so this stands one up in memory.

The fake ledger holds every row from the committed fixture, written under an
older config: two payees still carry their raw tokens and one row has no
category. That is exactly the state a rename leaves behind, which is the thing
the page exists to show.

Nothing here touches a real ledger, and the archive is a copy of the fixture in
a temporary directory.

    uv run --with playwright --with pyotp --with waitress \
        python scripts/shoot_reconcile.py r2
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pyotp
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from passbook import webauth  # noqa: E402
from shoot import DESKTOP, MOBILE, PASSWORD, SECRET, USER, sign_in  # noqa: E402

CHROME = os.environ.get("PW_CHROME")
FIXTURE = ROOT / "tests" / "fixtures" / "statement.xls"


def fake_client_class(asset_name: str, archive: Path):
    """A ledger stand-in holding the fixture's rows, one config behind."""
    from datetime import date
    from decimal import Decimal

    from passbook import service
    from passbook.push import build_split

    statements = service.archived_statements(archive)
    rows = []
    for index, txn in enumerate(statements[0].transactions if statements else []):
        split = build_split(txn, asset_name)
        # The stale part: the ledger keeps what it was written with. Strip the
        # category from every third row and leave the description as the raw
        # token, which is what a later alias would have replaced.
        split["category"] = "" if index % 3 == 0 else split.get("category", "")
        split["description"] = split["description"].replace(" (", " · (", 1)
        rows.append(split)

    class FakeLedger:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def close(self):
            return None

        def asset_accounts(self):
            # `current_balance` and `opening_balance` are not optional: the
            # Ledger asks for both on every sign-in, and a fake without them
            # raises on a page this script does not even shoot.
            return [
                {
                    "name": asset_name,
                    "current_balance": Decimal("5068.09"),
                    "opening_balance": Decimal("12612.64"),
                    "opening_on": date(2026, 5, 6),
                    "currency": "INR",
                }
            ]

        def account_transactions(self, account):
            return rows

        def identities(self, account):
            return {r["external_id"] for r in rows}

        def update_transaction(self, external_id, fields):
            return None

    return FakeLedger


def main() -> int:
    import threading

    from waitress import serve

    from passbook.web import create_app

    tag = sys.argv[1] if len(sys.argv) > 1 else "reconcile"
    out = ROOT / "docs" / "shots" / tag
    out.mkdir(parents=True, exist_ok=True)

    auth = webauth.WebAuth(
        username=USER,
        password_hash=webauth.hash_password(PASSWORD),
        totp_secret=SECRET,
        backup_codes=[],
    )

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        archive = root / "archive"
        (archive / "canara-1111").mkdir(parents=True)
        shutil.copy(FIXTURE, archive / "canara-1111" / "statement.xls")
        os.chdir(root)
        os.environ["PASSBOOK_ASSET_ACCOUNT"] = "Test Account"
        os.environ["PASSBOOK_ACCOUNT_NUMBER"] = "999900001111"

        # The seam is `_base`, not the package: a route resolves the name in its
        # own module's globals, so assigning the package attribute would be
        # silently ignored and every page would reach for a real store.
        from passbook.web.api import _base as api_base

        api_base.open_ledger = fake_client_class("Test Account", archive)

        app = create_app(
            {
                "WEB_AUTH_FIXED": auth,
                "SECRET_KEY": "screenshots",
                "INBOX": root / "inbox",
                "ARCHIVE": archive,
            }
        )
        (root / "inbox").mkdir(exist_ok=True)

        import socket

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        threading.Thread(
            target=lambda: serve(app, host="127.0.0.1", port=port, threads=4, _quiet=True),
            daemon=True,
        ).start()
        base = f"http://127.0.0.1:{port}"

        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME or None)
            for theme in ("light", "dark"):
                for size, viewport in (("desktop", DESKTOP), ("mobile", MOBILE)):
                    ctx = browser.new_context(
                        viewport=viewport, color_scheme=theme, device_scale_factor=2
                    )
                    page = ctx.new_page()
                    sign_in(page, base, auth)
                    page.goto(f"{base}{os.environ.get('SHOOT_ROUTE', '/reapply')}", wait_until="networkidle")
                    try:
                        page.wait_for_selector(".skel", state="detached", timeout=20000)
                    except Exception:
                        print(f"  !! reapply-{theme}-{size}: still loading after 20s")
                    page.wait_for_timeout(500)
                    shot = out / f"{os.environ.get('SHOOT_LABEL', 'reconcile')}-{theme}-{size}.png"
                    page.screenshot(path=str(shot), full_page=True)
                    print(f"  {shot.relative_to(ROOT)}")
                    ctx.close()
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
