#!/usr/bin/env python3
"""Generate the app icons from the Day Rail. SPEC §17.4.

The mark is the signature element, not a wallet and not a rupee glyph — those
belong to every finance app ever made. This is a rubber-stamp impression
containing a 24-hour track: the midnight-to-six band shaded, one transaction
tick standing in it. Abstract at 32px, and the only shape that is ours.

Maskable icons are rendered full-bleed on board with the mark inside the
central 80% safe zone, because Android and Chromium crop maskable icons to
whatever shape the launcher wants — a circle on most, a squircle on some.
Anything outside that circle can be cut off.

    uv run --with playwright python scripts/icons.py
"""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("frontend/public")
CHROME = os.environ.get(
    "PW_CHROME",
    str(Path.home() / ".cache/ms-playwright/chromium-1228/chrome-linux64/chrome"),
)

BOARD = "#3a4250"
STAMP = "#8f77e8"   # lifted off --stamp so it holds contrast on board
NIGHT = "#5c43a5"
TRACK = "#7d8698"


def mark(size: int, *, bleed: bool, scale: float) -> str:
    """One SVG. `bleed` fills the canvas (maskable); `scale` sizes the mark."""
    c = 256.0
    half = 320 * scale / 2
    stroke = 26 * scale
    r = 56 * scale
    # The track sits inside the stamp with its own margin.
    tw = half * 2 - stroke * 2 - 36 * scale
    th = 92 * scale
    tx, ty = c - tw / 2, c - th / 2
    night_w = tw * 0.25
    tick_w = 26 * scale
    tick_x = tx + tw * 0.56 - tick_w / 2

    background = (
        f'<rect width="512" height="512" fill="{BOARD}"/>'
        if bleed
        else f'<rect width="512" height="512" rx="96" fill="{BOARD}"/>'
    )
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="{size}" height="{size}">
  {background}
  <rect x="{c - half}" y="{c - half}" width="{half * 2}" height="{half * 2}"
        rx="{r}" fill="none" stroke="{STAMP}" stroke-width="{stroke}"/>
  <rect x="{tx}" y="{ty}" width="{tw}" height="{th}" rx="{10 * scale}" fill="{TRACK}"/>
  <rect x="{tx}" y="{ty}" width="{night_w}" height="{th}" rx="{10 * scale}" fill="{NIGHT}"/>
  <rect x="{tick_x}" y="{ty - 14 * scale}" width="{tick_w}" height="{th + 28 * scale}"
        rx="{8 * scale}" fill="{STAMP}"/>
</svg>"""


# A flat favicon: no board, so it reads on a browser tab of any colour.
FAVICON = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
  <rect width="512" height="512" rx="96" fill="{BOARD}"/>
  <rect x="96" y="96" width="320" height="320" rx="56" fill="none"
        stroke="{STAMP}" stroke-width="26"/>
  <rect x="158" y="210" width="196" height="92" rx="10" fill="{TRACK}"/>
  <rect x="158" y="210" width="49" height="92" rx="10" fill="{NIGHT}"/>
  <rect x="256" y="192" width="30" height="128" rx="8" fill="{STAMP}"/>
</svg>"""


def render(page, svg: str, size: int, path: Path) -> None:
    page.set_viewport_size({"width": size, "height": size})
    page.set_content(
        f'<body style="margin:0;width:{size}px;height:{size}px">{svg}</body>'
    )
    page.screenshot(path=path, omit_background=False)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "favicon.svg").write_text(FAVICON)

    jobs = [
        # maskable: full bleed, mark at 62% so it survives a circular crop
        ("icon-192-maskable.png", 192, True, 0.62),
        ("icon-512-maskable.png", 512, True, 0.62),
        # any: rounded plate, mark larger since nothing crops it
        ("icon-192.png", 192, False, 0.80),
        ("icon-512.png", 512, False, 0.80),
        ("apple-touch-icon.png", 180, False, 0.80),
    ]
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = browser.new_page()
        for name, size, bleed, scale in jobs:
            render(page, mark(size, bleed=bleed, scale=scale), size, OUT / name)
            print(f"  {name:28} {size}x{size}  {(OUT / name).stat().st_size // 1024:>3} KB")
        # At 32px the stamp outline crowds the rail into mush, so the small
        # favicon drops the frame and shows the rail alone — still the same
        # mark, just the part that survives the size.
        small = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="32" height="32">
          <rect width="512" height="512" rx="110" fill="{BOARD}"/>
          <rect x="86" y="196" width="340" height="120" rx="18" fill="{TRACK}"/>
          <rect x="86" y="196" width="85" height="120" rx="18" fill="{NIGHT}"/>
          <rect x="232" y="168" width="52" height="176" rx="14" fill="{STAMP}"/>
        </svg>'''
        render(page, small, 32, OUT / "favicon-32.png")
        print(f"  {'favicon-32.png':28} 32x32     {(OUT / 'favicon-32.png').stat().st_size // 1024:>3} KB")
        browser.close()
    print(f"  {'favicon.svg':28} vector    {(OUT / 'favicon.svg').stat().st_size} B")


if __name__ == "__main__":
    main()
