#!/usr/bin/env python3
"""Drive and observe the states a static screenshot cannot see. SPEC §17.9.

`shoot.py` captures settled pages, and it waits for loading to finish — so it
excludes skeletons, progress bars, toasts and transitions by construction.
Phase 11 shipped all of those and none had ever been seen running.

This drives each one and reports what was actually observed:

  * skeletons      — by stalling the API so the loading state persists
  * progress bars  — by stalling the upload and capturing mid-flight
  * toasts         — by completing an action and reading the toast text
  * errors         — by uploading a PDF and reading what it says to do
  * transitions    — by reading computed animation on a freshly routed page
  * reduced motion — by asking for it and confirming the animations stop

Frames land in docs/shots/motion/ (gitignored, same as the rest).

    uv run --with playwright --with pyotp python scripts/motion.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parent))

from shoot import CHROME, DESKTOP, PASSWORD, SECRET, USER, start  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from passbook import webauth  # noqa: E402

OUT = Path("docs/shots/motion")
findings: list[tuple[str, str]] = []


def note(label: str, observed: str) -> None:
    findings.append((label, observed))
    print(f"  {label:22} {observed}")


def sign_in(page, base: str, auth) -> None:
    import pyotp

    auth.totp_last_counter = None
    page.goto(f"{base}/", wait_until="domcontentloaded")
    page.fill("#username", USER)
    page.fill("#password", PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_selector("#code", timeout=15000)
    page.fill("#code", pyotp.TOTP(SECRET).now())
    page.click("button[type=submit]")
    page.wait_for_selector("nav", timeout=15000)


def animation_of(page, selector: str) -> str:
    # wait_for_selector first: eval_on_selector does not auto-wait, and reading
    # during the pre-paint frame returned empty strings for every animation
    # property — which read as "no transition shipped" when one had.
    page.wait_for_selector(selector, timeout=15000)
    page.wait_for_timeout(120)
    return page.eval_on_selector(
        selector,
        """el => {
             const s = getComputedStyle(el);
             return `${s.animationName} ${s.animationDuration} delay ${s.animationDelay}`;
           }""",
    )


def contrast(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    def channel(value: float) -> float:
        v = value / 255
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4

    def luminance(rgb):
        r, g, blue = (channel(c) for c in rgb)
        return 0.2126 * r + 0.7152 * g + 0.0722 * blue

    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def palette_report(browser, base: str, auth, scheme: str) -> str:
    """Read the computed surfaces and ramp back out of the browser.

    A screenshot shows whether the ramp looks right; this says whether the
    floor is above the point where a bar stops being a bar. The values are
    computed, not authored: `--ramp-*` are `color-mix()` expressions over
    `--ink` and `--sheet`, so each theme derives its own.
    """
    ctx = browser.new_context(viewport=DESKTOP, color_scheme=scheme)
    page = ctx.new_page()
    sign_in(page, base, auth)
    page.goto(f"{base}/", wait_until="domcontentloaded")
    page.wait_for_selector(".bars__fill", timeout=20000)
    # Resolved through a canvas, not read as a string. The ramp is `color-mix(in
    # oklab, ...)`, and Chromium's computed value for that is `oklab(L a b)` —
    # which is the correct answer to a different question. Painting it and
    # sampling the pixel gives the sRGB the operator actually sees. The sentinel
    # catches a value the canvas cannot parse, so an unsupported colour reads as
    # "unresolved" instead of silently as black.
    values = page.evaluate(
        """() => {
             const probe = document.createElement('span');
             document.body.appendChild(probe);
             const canvas = document.createElement('canvas');
             const paint = canvas.getContext('2d', { willReadFrequently: true });
             const read = (name) => {
               probe.style.color = `var(${name})`;
               const value = getComputedStyle(probe).color;
               paint.fillStyle = '#ff00ff';
               paint.fillStyle = value;
               paint.fillRect(0, 0, 1, 1);
               const [r, g, b] = paint.getImageData(0, 0, 1, 1).data;
               return (r === 255 && g === 0 && b === 255) ? null : [r, g, b];
             };
             const out = {};
             for (const name of ['--paper','--sheet','--band','--board','--grid',
                                 '--ramp-1','--ramp-2','--ramp-3','--ramp-4',
                                 '--ramp-5','--ramp-out']) out[name] = read(name);
             probe.remove();
             return out;
           }"""
    )
    ctx.close()

    def rgb(value) -> tuple[float, float, float]:
        if value is None:
            raise ValueError("a token did not resolve to a paintable colour")
        return tuple(float(channel) for channel in value)

    sheet = rgb(values["--sheet"])
    parts = [
        f"paper/sheet {contrast(rgb(values['--paper']), sheet):.2f}",
        f"board/sheet {contrast(rgb(values['--board']), sheet):.2f}",
        f"band/sheet {contrast(rgb(values['--band']), sheet):.2f}",
    ]
    ramp = [
        f"{contrast(rgb(values[f'--ramp-{n}']), sheet):.2f}" for n in (1, 2, 3, 4, 5)
    ]
    parts.append("ramp vs card " + " ".join(ramp))
    parts.append(f"excluded {contrast(rgb(values['--ramp-out']), sheet):.2f}")
    return "; ".join(parts)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    inbox = Path(tempfile.mkdtemp(prefix="motion-inbox-"))
    auth = webauth.WebAuth(
        username=USER,
        password_hash=webauth.hash_password(PASSWORD),
        totp_secret=SECRET,
        totp_enrolled_at="2026-08-10T00:00:00+00:00",
        salt="0" * 32,
    )
    base = f"http://127.0.0.1:{start(auth, inbox)}"
    statement = sorted(Path("archive").rglob("*.xls"))[0]

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = browser.new_context(viewport=DESKTOP, color_scheme="light", device_scale_factor=2)
        page = ctx.new_page()
        sign_in(page, base, auth)

        # --- 1. skeletons: slow the network, not the test thread -----------
        # `time.sleep()` inside a sync route handler blocks Playwright's own
        # driver, so the stall and the screenshot could never overlap — the
        # capture always landed after the data arrived. CDP latency slows the
        # browser while this thread keeps running.
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")
        cdp.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 4000, "downloadThroughput": -1,
             "uploadThroughput": -1},
        )
        page.goto(f"{base}/payees", wait_until="commit")
        page.wait_for_selector(".skel", timeout=30000)
        skeletons = page.locator(".skel").count()
        spinners = page.locator(".spinner").count()
        page.screenshot(path=OUT / "skeleton-payees.png")
        note("skeletons", f"{skeletons} .skel visible at capture, {spinners} spinners")
        cdp.send(
            "Network.emulateNetworkConditions",
            {"offline": False, "latency": 0, "downloadThroughput": -1,
             "uploadThroughput": -1},
        )
        page.wait_for_selector("table.payees", timeout=30000)

        # --- 2. page transition --------------------------------------------
        page.goto(f"{base}/status", wait_until="domcontentloaded")
        note("page transition", animation_of(page, ".page"))

        # --- 2b. the charts drawing themselves -----------------------------
        # New in Phase 13, and exactly the kind of thing shoot.py cannot see:
        # it waits for loading to finish, which is when every one of these has
        # already finished running.
        page.goto(f"{base}/", wait_until="domcontentloaded")
        note("category bars", animation_of(page, ".bars__fill"))
        note(
            "bar stagger",
            "delays "
            + str(
                page.eval_on_selector_all(
                    ".bars__fill",
                    "els => els.slice(0, 6).map(e => getComputedStyle(e).animationDelay)",
                )
            ),
        )
        note("flow bar", animation_of(page, ".flow__counted"))
        note("excluded (hatched)", animation_of(page, ".flow__excluded"))
        note("month columns", animation_of(page, ".cols rect.col"))
        note(
            "column stagger",
            "delays "
            + str(
                page.eval_on_selector_all(
                    ".cols rect.col",
                    "els => els.map(e => getComputedStyle(e).animationDelay)",
                )
            ),
        )
        note("ledger-wide day rail", animation_of(page, ".hist rect.bar"))
        page.screenshot(path=OUT / "ledger-settled.png", full_page=True)

        # --- 2c. the palette, measured rather than admired -----------------
        # The ramp is `color-mix(--ink -> --sheet)`, so the shipped values are
        # whatever the browser computes — not what the CSS says. This reads them
        # back and states the contrast against the card, because the smallest
        # category still has to be visible and "looks fine" is not a number.
        for scheme in ("light", "dark"):
            note(f"palette ({scheme})", palette_report(browser, base, auth, scheme))

        # --- 3. Day Rail entrance, and that the stagger is real -------------
        page.goto(f"{base}/upload", wait_until="domcontentloaded")
        page.set_input_files("#statement", str(statement))
        page.click("button[type=submit]")
        page.wait_for_url("**/preview", timeout=60000)
        page.wait_for_selector(".rail__tick", timeout=20000)
        first = animation_of(page, ".rail__tick")
        delays = page.eval_on_selector_all(
            ".rail__tick",
            "els => els.slice(0, 6).map(e => getComputedStyle(e).animationDelay)",
        )
        note("day rail tick", first)
        note("stagger", f"first six delays {delays}")
        page.screenshot(path=OUT / "preview-settled.png")

        # --- 4. toast wording, after a real push-adjacent action ------------
        # Discard is the safe one: it changes nothing in Firefly.
        page.click("text=Discard")
        page.wait_for_selector(".toast", timeout=10000)
        page.wait_for_timeout(400)
        # LAST, not first: the "Checked" toast from the upload is still on
        # screen for five seconds, so `.first` read the previous action's
        # confirmation and reported it as this one's.
        titles = page.locator(".toast__title").all_inner_texts()
        title = page.locator(".toast__title").last.inner_text()
        detail = page.locator(".toast__detail").last.inner_text()
        page.screenshot(path=OUT / "toast-discarded.png")
        note("toasts on screen", f"{titles}")
        note("toast (Discard)", f"title={title!r} detail={detail!r}")

        # --- 5. progress bar, captured mid-flight ---------------------------
        page.goto(f"{base}/upload", wait_until="domcontentloaded")
        page.route("**/api/statement", lambda route: (__import__("time").sleep(2.5), route.continue_()))
        page.set_input_files("#statement", str(statement))
        page.click("button[type=submit]")
        page.wait_for_selector(".progress", timeout=8000)
        label = page.locator(".progress").get_attribute("aria-label")
        button = page.locator("button[type=submit]").inner_text()
        page.screenshot(path=OUT / "progress-upload.png")
        note("progress", f"aria-label={label!r}, button reads {button!r}")
        page.unroute("**/api/statement")
        page.wait_for_url("**/preview", timeout=60000)
        page.goto(f"{base}/preview", wait_until="domcontentloaded")
        try:
            page.click("text=Discard", timeout=5000)
        except Exception:
            pass

        # --- 6. an error that says what to do -------------------------------
        pdf = inbox / "statement.pdf"
        pdf.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\ntrailer<<>>\n")
        page.goto(f"{base}/upload", wait_until="domcontentloaded")
        page.set_input_files("#statement", str(pdf))
        page.click("button[type=submit]")
        page.wait_for_selector(".toast--bad", timeout=15000)
        err = page.locator(".toast--bad .toast__detail").first.inner_text()
        contextual = page.locator(".notice--warn").count()
        page.screenshot(path=OUT / "error-pdf.png", full_page=True)
        note("error toast", err[:120])
        note("contextual PDF warning", f"{contextual} ochre notice(s) on the page")
        ctx.close()

        # --- 7. reduced motion ----------------------------------------------
        rm = browser.new_context(
            viewport=DESKTOP, color_scheme="light", reduced_motion="reduce"
        )
        rpage = rm.new_page()
        sign_in(rpage, base, auth)
        rpage.goto(f"{base}/status", wait_until="domcontentloaded")
        rpage.wait_for_selector(".page", timeout=15000)
        note("reduced motion .page", animation_of(rpage, ".page"))
        # The charts are new, so their reduced-motion behaviour is new too: every
        # one of them has to be `none`, not merely fast.
        rpage.goto(f"{base}/", wait_until="domcontentloaded")
        for label, selector in (
            ("bars", ".bars__fill"),
            ("flow", ".flow__counted"),
            ("columns", ".cols rect.col"),
            ("day rail", ".hist rect.bar"),
        ):
            note(f"reduced motion {label}", animation_of(rpage, selector))
        rpage.screenshot(path=OUT / "ledger-reduced-motion.png", full_page=True)
        rm.close()
        browser.close()

    shutil.rmtree(inbox, ignore_errors=True)
    print(f"\n  frames -> {OUT}")


if __name__ == "__main__":
    main()
