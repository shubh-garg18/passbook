"""Static regression tests for things that fail silently. SPEC §17.

None of these needs a browser or a running stack — they read the files that
ship. Each one exists because the failure it catches produces **no error**:

* a manifest served as `application/octet-stream` makes "Install" quietly
  never appear;
* a CSS specificity accident repaints an unrelated table and nothing warns;
* a Caddyfile without `auto_https off` starts fine and then tries to mint a
  certificate.

The rendered counterparts live in `test_stack.py`, which needs the stack up.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CSS = ROOT / "frontend/src/theme.css"
MANIFEST = ROOT / "frontend/public/manifest.webmanifest"
CADDYFILE = ROOT / "Caddyfile"
PUBLIC = ROOT / "frontend/public"


# --- 1. the manifest -------------------------------------------------------


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_is_valid_json_with_the_fields_install_requires(manifest):
    assert manifest["name"]
    assert manifest["start_url"]
    assert manifest["display"] == "standalone", "anything else installs as a tab"
    sizes = {i["sizes"] for i in manifest["icons"]}
    assert {"192x192", "512x512"} <= sizes, f"Chromium wants both; got {sizes}"


def test_manifest_ships_maskable_icons_at_both_sizes(manifest):
    """Without `purpose: maskable` a launcher crops the square plate and the
    icon arrives with its corners sliced off."""
    maskable = {i["sizes"] for i in manifest["icons"] if i["purpose"] == "maskable"}
    assert maskable == {"192x192", "512x512"}


def test_every_icon_the_manifest_names_actually_exists(manifest):
    missing = [i["src"] for i in manifest["icons"] if not (PUBLIC / i["src"].lstrip("/")).is_file()]
    assert not missing, f"manifest points at files that are not there: {missing}"


def test_the_theme_colour_matches_the_board_token():
    """The installed window's title bar is painted with this. If it drifts from
    --board the app opens with a strip of the wrong grey above the cover."""
    board = re.search(r"^\s*--board:\s*(#[0-9a-fA-F]{6});", CSS.read_text(), re.M)
    assert board, "--board is gone from theme.css"
    manifest_theme = json.loads(MANIFEST.read_text())["theme_color"]
    assert manifest_theme.lower() == board.group(1).lower()


# --- 2. Caddy, statically --------------------------------------------------


def test_caddy_disables_automatic_https():
    """Left on, Caddy mints a cert from its internal CA for a `.localhost`
    name and tries to install that CA in the system trust store."""
    assert re.search(r"^\s*auto_https\s+off\s*$", CADDYFILE.read_text(), re.M)


def _site_block(host: str) -> str:
    """The body of `http://<host> { … }`, comments stripped.

    Naive splitting matched the explanatory comment block at the top of the
    file instead of the site block, so this walks braces.
    """
    text = re.sub(r"(?m)^\s*#.*$", "", CADDYFILE.read_text())
    start = text.index(f"http://{host}")
    open_brace = text.index("{", start)
    depth, index = 0, open_brace
    for index in range(open_brace, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                break
    return text[open_brace + 1 : index]


def test_caddy_routes_both_hosts_over_plain_http():
    text = CADDYFILE.read_text()
    assert "http://passbook.localhost" in text, "the host must be pinned to http://"
    assert "http://khata.localhost" in text
    # Swapping these would serve Firefly at the passbook name, which presents
    # as a login loop rather than as a routing bug.
    assert "web:8081" in _site_block("passbook.localhost")
    assert "app:8080" in _site_block("khata.localhost")


def test_firefly_gets_the_forwarded_headers_it_needs():
    """Firefly builds absolute URLs from APP_URL and reads these when
    TRUSTED_PROXIES is set. Without them a login redirect bounces the browser
    back to :8080 and off the clean hostname."""
    khata = _site_block("khata.localhost")
    for header in ("X-Forwarded-Host", "X-Forwarded-Proto"):
        assert header in khata, f"{header} missing from the khata route"


def test_the_numbered_ports_are_still_published():
    """8080 and 8081 are what the runbook, the DR drill and every healthcheck
    use, and they run when Caddy may not be up.

    Firefly's *host* port is settable — a Windows service on 8080 makes the bind
    fail outright under WSL mirrored networking — so what is pinned is the
    default and the container port, not a literal. 8081 and 80 stay literal
    because nothing has needed to move them."""
    compose = (ROOT / "docker-compose.yml").read_text()
    assert '"127.0.0.1:${FIREFLY_HOST_PORT:-8080}:8080"' in compose
    assert '"127.0.0.1:8081:8081"' in compose
    assert '"127.0.0.1:80:80"' in compose


def test_nothing_is_bound_beyond_loopback():
    """D9. A published port without the 127.0.0.1 prefix listens on 0.0.0.0."""
    compose = (ROOT / "docker-compose.yml").read_text()
    published = re.findall(r'^\s*- "([^"]+)"\s*$', compose, re.M)
    # `${VAR:-default}` counts as a port spec. Written as `\d+` this expression
    # quietly dropped the app's line the moment its host port became settable,
    # and a mapping this check cannot see is a mapping it cannot hold to
    # loopback — the failure would have been silent and in the unsafe direction.
    ports = [p for p in published if re.match(r"^[\d.]*:?[\d${}:a-zA-Z_-]+:\d+$", p)]
    assert len(ports) >= 3, f"expected the app, web and caddy mappings, found {ports}"
    for port in ports:
        assert port.startswith("127.0.0.1:"), f"{port} is reachable beyond loopback"


# --- 3. banding scope ------------------------------------------------------


def _rules(css: str) -> list[tuple[str, str]]:
    """(selector, declarations) for every rule, including inside @media.

    Written by hand rather than with a parser dependency: the file is one
    stylesheet this project owns, and the shapes in it are known.
    """
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    out: list[tuple[str, str]] = []
    depth = 0
    buffer = ""
    selector = ""
    for char in css:
        if char == "{":
            depth += 1
            if depth == 1 or (depth == 2 and selector.strip().startswith("@")):
                selector = buffer.strip()
                buffer = ""
            elif depth == 2:
                selector = buffer.strip()
                buffer = ""
            continue
        if char == "}":
            if depth >= 1 and selector and not selector.startswith("@"):
                out.append((selector, buffer))
            depth -= 1
            buffer = ""
            selector = ""
            continue
        buffer += char
    return out


def _specificity(selector: str) -> tuple[int, int, int]:
    """(ids, classes+attrs+pseudo-classes, elements). Good enough for this
    stylesheet: no ids, no `:where()`, no `:is()`."""
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = len(re.findall(r"\.[\w-]+|\[[^\]]+\]|:(?!:)[\w-]+", selector))
    elements = len(re.findall(r"(?:^|[\s>+~])([a-z][\w-]*)", selector))
    return ids, classes, elements


def test_row_banding_is_scoped_to_the_data_sheets():
    """The accident this pins: `tbody tr:nth-child(odd) td` (0,1,3) quietly
    out-specified `.kv tr td` (0,1,2), so ledger banding repainted every
    key-value table — which is what made "Warnings: none" read as a success
    state. Banding belongs to the ledger and the payee table, nowhere else.
    """
    offenders = []
    for selector, decls in _rules(CSS.read_text()):
        if "nth-child(odd)" not in selector:
            continue
        if "background" not in decls:
            continue
        for part in selector.split(","):
            part = part.strip()
            if not part:
                continue
            if not (".ledger" in part or ".payees" in part):
                offenders.append(part)
    assert not offenders, (
        "unscoped row banding will leak into .kv and any other table: " f"{offenders}"
    )


def test_kv_cells_outrank_every_generic_cell_background():
    """Scoping alone is not the invariant — the invariant is that nothing
    unscoped can out-specify the rule that keeps key-value tables plain."""
    css = CSS.read_text()
    kv = max(
        (_specificity(part.strip())
         for selector, decls in _rules(css)
         if "background" in decls
         for part in selector.split(",")
         if ".kv" in part),
        default=None,
    )
    assert kv is not None, ".kv no longer sets a background — this test is stale"

    for selector, decls in _rules(css):
        if "background" not in decls:
            continue
        for part in selector.split(","):
            part = part.strip()
            if not part or ".kv" in part:
                continue
            # Only rules that could match a cell inside a .kv table matter.
            if not re.search(r"(^|[\s>+~])(td|th|tr|tbody)\b", part):
                continue
            if ".ledger" in part or ".payees" in part:
                continue  # scoped away from .kv by construction
            assert _specificity(part) < kv, (
                f"{part!r} can out-specify the .kv rule and repaint key-value tables"
            )


# --- 4. the palette discipline ---------------------------------------------
# SPEC §18. A category chart wants ten distinguishable fills and this palette has
# exactly three colours that mean anything. The answer was one ink at five
# densities, ordered by magnitude — which only stays an answer if nothing quietly
# reaches for the semantic colours later.

CHARTS = ROOT / "frontend/src/components/charts.tsx"


def test_the_ramp_is_defined_and_derives_from_the_theme_it_is_in():
    """Both themes get the ramp, and both derive it from their OWN ink and card
    rather than from literals — otherwise dark mode ends up with light mode's
    bars, which is the failure mode the flat dark palette already had."""
    css = CSS.read_text()
    for step in (1, 2, 3, 4, 5):
        assert f"--ramp-{step}:" in css, f"--ramp-{step} is missing"
        line = next(l for l in css.splitlines() if f"--ramp-{step}:" in l)
        assert "color-mix" in line and "var(--ink)" in line and "var(--sheet)" in line, line
    assert "--ramp-out:" in css, "the excluded-remainder fill is missing"


def test_no_chart_mark_uses_a_colour_that_means_something():
    """Ochre means "needs your decision", verdigris means "reconciled", stamp
    means "this acts". A chart fill wearing any of them makes the one colour that
    carried meaning mean nothing — which is the mistake §17.2 and §17.5.1 each
    had to undo once already.

    `--alarm` is included: a bar is not a failure.
    """
    source = CHARTS.read_text()
    for token in ("--ochre", "--verdigris", "--alarm", "--stamp"):
        assert token not in source, (
            f"{CHARTS.name} references {token}; chart marks use --ramp-* only"
        )


def test_the_charts_carry_no_library():
    """The Day Rail primitive was already here, and 90 KB of chart library would
    outweigh every font this app ships (46 KB for six faces)."""
    package = json.loads((ROOT / "frontend/package.json").read_text())
    dependencies = {**package.get("dependencies", {}), **package.get("devDependencies", {})}
    for banned in ("recharts", "chart.js", "d3", "victory", "nivo", "plotly.js", "apexcharts"):
        assert not any(banned in name for name in dependencies), (
            f"a charting library ({banned}) crept into package.json"
        )


def test_the_page_and_the_card_are_actually_different_values():
    """The Phase 11 defect, in the other direction: cards that do not read as
    sitting on anything. Pinned as a relationship rather than a pair of hexes, so
    a future tweak has to keep the gap."""
    css = CSS.read_text()

    def token(name: str, block: str) -> str:
        section = css.split("prefers-color-scheme: dark")[1 if block == "dark" else 0]
        return re.search(rf"{name}:\s*(#[0-9a-fA-F]{{6}})", section).group(1)

    def luminance(hex_colour: str) -> float:
        channels = []
        for index in (1, 3, 5):
            value = int(hex_colour[index : index + 2], 16) / 255
            channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    for theme in ("light", "dark"):
        paper, sheet = token("--paper", theme), token("--sheet", theme)
        high, low = sorted((luminance(paper), luminance(sheet)), reverse=True)
        ratio = (high + 0.05) / (low + 0.05)
        assert ratio > 1.15, f"{theme}: card and page are {ratio:.2f}:1 apart — too close to see"


def test_no_ui_string_pluralises_with_a_parenthesis():
    """SPEC §17.5.2: `day(s)` is a form field, not a sentence.

    That was fixed in one string and left applying to one string — seven others
    were still writing `device(s)`, `duplicate(s)`, `row(s)`, the last of them on
    a red button that deletes and re-pushes the ledger. `lib/money.count` is the
    replacement, so this pins the rule against the next one.

    Scoped to a letter immediately before `(s)`, which is what the pattern looks
    like in prose; `map((s) => …)` and other code shapes are unaffected.
    """
    offenders = []
    for path in sorted((ROOT / "frontend/src").rglob("*.tsx")):
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if re.search(r"\w\(s\)", line):
                offenders.append(f"{path.name}:{number}: {line.strip()[:70]}")
    assert not offenders, "use lib/money.count instead: " + "; ".join(offenders)
