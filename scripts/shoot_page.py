#!/usr/bin/env python3
"""Shoot ONE route, both themes, both widths — no ledger, no database.

`shoot.py` walks the whole app and stages a statement on the way, so it needs a
running stack even to reach a page that reads nothing. A page that renders with
the stack down should be verifiable with the stack down; that is also the state
a new user is in the first time they need Add a bank.

    uv run --with playwright --with pyotp --with waitress \
        python scripts/shoot_page.py /banks/add add-bank r1

Add `--hero` for a README picture: the viewport only, at 1x. A full-page
capture of a long page is a ten-thousand-pixel image, which is a wall a reader
scrolls past rather than a picture they look at.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pyotp
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from passbook import webauth  # noqa: E402
from shoot import DESKTOP, MOBILE, PASSWORD, SECRET, USER, sign_in, start  # noqa: E402

CHROME = os.environ.get("PW_CHROME")


def main() -> int:
    route, label, tag = sys.argv[1], sys.argv[2], sys.argv[3]
    # `--hero` captures the VIEWPORT rather than the whole page.
    #
    # A full-page capture of a long page is a 10,000-pixel-tall image, and in a
    # README that is a wall a reader scrolls past rather than a picture they
    # look at. The first screen is what a screenshot is for; the rest of the
    # page is what the app is for.
    hero = "--hero" in sys.argv
    out = ROOT / "docs" / "shots" / tag
    out.mkdir(parents=True, exist_ok=True)

    auth = webauth.WebAuth(
        username=USER,
        password_hash=webauth.hash_password(PASSWORD),
        totp_secret=SECRET,
        # A full set, so the "0 backup codes left" warning does not appear in
        # a picture of a healthy ledger. The warning is correct and wanted on a
        # real install; it is demo furniture in a screenshot of a feature.
        backup_codes=[webauth.hash_password(f"code-{i}") for i in range(8)],
    )
    # A hero shot is for a README, where one image per page is the point, and
    # at 1x it is already 1440 wide — plenty, and a quarter of the bytes.
    scale = 1 if hero else 2

    with tempfile.TemporaryDirectory() as tmp:
        port = start(auth, Path(tmp))
        base = f"http://127.0.0.1:{port}"
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=CHROME or None)
            for theme in ("light", "dark"):
                for size, viewport in (("desktop", DESKTOP), ("mobile", MOBILE)):
                    ctx = browser.new_context(
                        viewport=viewport, color_scheme=theme, device_scale_factor=scale
                    )
                    page = ctx.new_page()
                    sign_in(page, base, auth)
                    page.goto(f"{base}{route}", wait_until="networkidle")
                    # Wait for the skeleton to go, and SAY SO if it does not.
                    # A harness that shoots whatever is on screen at 400ms
                    # photographs the loading state and calls it the page.
                    try:
                        page.wait_for_selector(
                            ".skel", state="detached", timeout=15000
                        )
                    except Exception:
                        print(f"  !! {label}-{theme}-{size}: still loading after 15s")
                    page.wait_for_timeout(400)
                    shot = out / f"{label}-{theme}-{size}.png"
                    page.screenshot(path=str(shot), full_page=not hero)
                    print(f"  {shot.relative_to(ROOT)}")
                    ctx.close()
            browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
