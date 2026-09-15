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


def test_caddy_routes_the_one_host_over_plain_http():
    text = CADDYFILE.read_text()
    assert "http://passbook.localhost" in text, "the host must be pinned to http://"
    assert "web:8081" in _site_block("passbook.localhost")


def test_there_is_only_one_door():
    """A second hostname once served a separate ledger application's UI beside
    this one, and the operator ran both because passbook had no charts, no
    reports and no transaction browser. It has all three, and the instruction
    was explicit: *"I want only one localhost and it have all required
    features"*. That application is gone entirely now, so the block it was
    commented out as is gone too.

    Asserted on the whole file, uncommented or not: there is nothing left to
    bring back for a debugging session.
    """
    text = CADDYFILE.read_text()
    assert "khata.localhost" not in text
    assert "app:8080" not in text, "there is no second application to proxy to"


def test_a_fresh_install_is_welcomed_rather_than_diagnosed():
    """The first screen anyone sees, and it used to be a fault report.

    On somebody's very first sign-in the Ledger page opened with `BALANCE
    unavailable` in red, `Ledger unverified`, `No backup`, and the sentence
    *"Set the missing value in .env on the host, then reload."* All of it
    accurate; all of it wrong. Nothing is broken on a fresh install and nothing
    needs editing — there is one thing to do, and the page now says it.

    Asserted on the source because there is nothing to render against: the
    branch fires when the account list comes back empty, which is a state no
    fixture produces.
    """
    home = (ROOT / "frontend/src/pages/Home.tsx").read_text()
    assert "accounts.length === 0" in home, "the fresh-install branch is gone"
    assert "<FirstRun />" in home
    # The three things it must not say to someone who has done nothing wrong.
    welcome = home[home.index("function FirstRun()"):]
    welcome = welcome[: welcome.index("\n}\n")]
    for forbidden in (".env", "unavailable", "unverified"):
        assert forbidden not in welcome, f"the welcome mentions {forbidden!r}"
    assert "Upload a statement" in welcome, "it has to say the one thing to do"


def test_the_numbered_ports_are_still_published():
    """8081 is what the runbook, the DR drill and every healthcheck use, and
    they run when Caddy may not be up.

    The database's *host* port is settable — a Postgres already installed on
    this machine makes the bind fail outright — so what is pinned there is the
    default and the container port, not a literal. 8081 and 80 stay literal
    because nothing has needed to move them."""
    compose = (ROOT / "docker-compose.yml").read_text()
    assert '"127.0.0.1:${PASSBOOK_DB_PORT:-5433}:5432"' in compose
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
            # Scoped away from .kv by construction: each of these names a
            # specific table, and a key-value table is never nested inside one.
            # `.txns` (§61) is the same case as the other two, not a new
            # exemption — if a .kv ever appears inside one of these, the
            # exclusion is what has to go, not the assertion.
            if any(name in part for name in (".ledger", ".payees", ".txns")):
                continue
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


# The one documented exception, and the reasoning that earns it. §90.
#
# `--ochre` means "this wants your attention". The excluded-movement bar is
# precisely the money the headline figure is asking you to notice it left out,
# so ochre there is the rule working rather than the rule breaking — and it is
# hatched, which no status indicator ever is. Everything else stays reserved.
OCHRE_EXCEPTION = ".bars__fill--out", ".flow__excluded"


def test_no_chart_mark_uses_a_colour_that_means_something():
    """Ochre means "needs your decision", verdigris means "reconciled", stamp
    means "this acts". A chart fill wearing any of them makes the one colour
    that carried meaning mean nothing — the mistake §17.2 and §17.5.1 each had
    to undo once already. `--alarm` is included: a bar is not a failure.

    **Checks USAGE, not mentions.** The first version scanned for the bare
    token and so failed on a comment explaining why a token was *not* used —
    which punishes exactly the documentation this codebase runs on. It now
    looks for `var(--token)` in a value, and it covers `theme.css` as well as
    the component: the fills moved to CSS and the check did not follow them.
    """
    reserved = ("--ochre", "--verdigris", "--alarm", "--stamp")

    source = CHARTS.read_text()
    for token in reserved:
        assert f"var({token})" not in source, (
            f"{CHARTS.name} fills a mark with {token}; chart marks use --cat-* or --ramp-*"
        )

    # In CSS, look only inside the rules that paint a mark.
    css = CSS.read_text()
    for selector, body in re.findall(r"([^{}]+)\{([^}]*)\}", css):
        name = selector.strip().split("\n")[-1].strip()
        # Named explicitly, not pattern-matched. `__bar` caught
        # `.progress__bar`, which is a CONTROL and for which `--stamp` — "this
        # acts" — is exactly the right ink. A loose heuristic that flags a
        # correct usage teaches people to widen the exception list.
        if not any(
            k in name
            for k in (
                ".bars__fill",
                ".stack__seg",
                ".flow__counted",
                ".flow__excluded",
                ".cols rect.col",
                ".hist rect.bar",
                ".donut__arc",
                ".week__bar",
                ".heat__bar",
                ".line__path",
            )
        ):
            continue
        if any(allowed in name for allowed in OCHRE_EXCEPTION):
            continue
        for token in reserved:
            assert f"var({token})" not in body, (
                f"{name} fills a mark with {token} — reserved for meaning, not for data"
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

    # §59 inverted the file (dark is the default block) and §78 moved the light
    # branch from a media query onto `:root[data-mode='light']`, so the mode is
    # always stamped and a bank theme can default to light on a dark OS. The
    # assertion below is untouched — only where each theme's values live moved.
    def token(name: str, block: str) -> str:
        section = css.split(":root[data-mode='light']")[1 if block == "light" else 0]
        match = re.search(rf"{name}:\s*(#[0-9a-fA-F]{{6}})", section)
        assert match, f"{name} not found in the {block} block"
        return match.group(1)

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
            stripped = line.strip()
            # A comment cannot render, and quoting a server message verbatim in
            # one is worth more than the false positive it costs — §41.2's
            # comment quotes `unknown field(s): [...]` because that is the
            # string an operator actually saw.
            if stripped.startswith(("//", "*", "/*")):
                continue
            if re.search(r"\w\(s\)", line):
                offenders.append(f"{path.name}:{number}: {stripped[:70]}")
    assert not offenders, "use lib/money.count instead: " + "; ".join(offenders)


def test_both_themes_declare_the_same_token_set():
    """§28. Two themes, dark and light, and each is a
    `:root[data-mode='…']` block.

    A token declared in one branch and not the other keeps whatever the other
    branch left it at. That shipped once: bank themes set `--sheet` and
    `--board` in dark only, `[data-theme]` matched `:root`'s specificity and
    sat later in the file, and light mode got navy cards under near-black ink
    at about 1.15:1. A media query would not have saved it — media queries add
    no specificity.
    """
    css = CSS.read_text()
    blocks = {}
    for mode, body in re.findall(r":root\[data-mode='([a-z]+)'\] \{([^}]*)\}", css):
        blocks.setdefault(mode, set()).update(re.findall(r"(--[a-z0-9-]+)\s*:", body))

    # The dark values live on bare `:root`, so only light is an attribute block;
    # what matters is that every token light overrides also exists in the base.
    assert "light" in blocks, "no light theme found — this test is stale"
    base = set(re.findall(r"(--[a-z0-9-]+)\s*:", css.split(":root[data-mode='light']")[0]))
    orphans = blocks["light"] - base
    assert not orphans, (
        f"light declares {sorted(orphans)} that the dark default never sets — "
        f"those will be undefined in dark mode"
    )
    for needed in ("--stamp", "--stamp-ink", "--sheet", "--paper", "--ink", "--cat-1"):
        assert needed in blocks["light"], f"light does not override {needed}"


def test_no_theme_block_touches_the_reserved_signals_or_the_chart_wheel():
    """A theme may move the accent and the surfaces. It may never move a
    signal, because `--ochre` asks, `--verdigris` reconciles and `--alarm`
    failed — and the chart hues were measured against these grounds.

    The light theme is the one exception the rule is written around: it is a
    whole second palette, not an overlay, so it redefines everything. What must
    not happen is a THIRD block appearing that redefines a signal partially.
    """
    css = CSS.read_text()
    partial = re.findall(r"\[data-theme='([a-z]+)'\][^{]*\{([^}]*)\}", css)
    for name, body in partial:
        for reserved in ("--ochre", "--verdigris", "--alarm", "--cat-", "--ramp-"):
            assert reserved not in body, f"{name} overrides {reserved}, which is reserved"


def test_every_routed_page_is_screenshotted():
    """§75. Transactions and Reports both shipped without being in `shoot.py`'s
    page list — two new pages that the harness which exists so pages get LOOKED
    at never looked at. Routes and shots drift silently; this makes them not.

    Redirect-only and flow-only routes are exempt by name, because a shot of a
    redirect is a shot of wherever it went.
    """
    app_tsx = (ROOT / "frontend/src/App.tsx").read_text()
    shoot = (ROOT / "scripts/shoot.py").read_text()

    routed = set(re.findall(r'<Route path="(/[^"*]*)"', app_tsx))
    # Reached only by completing a flow, and each already has its own shot or
    # is covered by the harness's own scripted journeys.
    exempt = {
        "/", "/preview", "/result", "/payees/diff", "/reapply/done",
        "/accounts/add", "/banks/add", "/password",
    }
    for route in sorted(routed - exempt):
        assert f'("{route}"' in shoot, (
            f"{route} is routed but never screenshotted — add it to shoot.py's PAGES"
        )


def test_a_bank_theme_sets_the_same_tokens_in_light_and_dark():
    """§77. The dark blocks set `--sheet`, `--board` and `--grid-soft`; the
    light overrides did not. `[data-theme]` has the same specificity as `:root`
    and sits later in the file, so those dark surfaces won in LIGHT mode too —
    a light-mode operator who picked a bank accent got navy cards under
    near-black ink, measured at about 1.15:1.

    A media query adds no specificity. Anything a theme overrides in one branch
    it must override in the other.
    """
    css = CSS.read_text()
    blocks: dict[str, list[set[str]]] = {}
    for name, body in re.findall(
        r"\[data-theme='([a-z]+)'\]\[data-mode='[a-z]+'\]\s*\{([^}]*)\}", css
    ):
        blocks.setdefault(name, []).append(set(re.findall(r"(--[a-z-]+)\s*:", body)))

    for name, declared in blocks.items():
        assert len(declared) == 2, f"{name} is declared {len(declared)} time(s)"
        dark, light = declared
        missing = dark - light
        assert not missing, (
            f"{name} sets {sorted(missing)} in dark and not in light — those dark "
            f"values will win in light mode too"
        )
        assert not (light - dark), f"{name} sets {sorted(light - dark)} only in light"


# --- the account switcher's one shared value. SPEC §100 -----------------------

ACCOUNT_TS = ROOT / "frontend/src/lib/account.ts"


def _without_comments(text: str) -> str:
    """The code, with `/* … */` and `// …` removed. Prose is not usage."""
    return re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", text, flags=re.S))


def test_the_account_selection_is_not_per_component_state():
    """`useAccounts()` is called by the tab strip, by the Combine checklist and
    by every page that scopes a query. Held in `useState`, each of those gets
    its OWN copy — so clicking a tab moved that tab strip's highlight, wrote
    localStorage, and told nobody. The page underneath kept querying the account
    it was already showing.

    Not catchable by a screenshot, which shows a settled page, and not by an API
    test, which never renders two components. A single-account install renders
    no switcher at all, so nothing could reveal it until a second account was
    registered.

    Asserted on the source because there is no frontend test runner here. It is
    a narrow claim — the selection is read from a store every caller shares, not
    from local state — and it is exactly the property that broke.
    """
    text = ACCOUNT_TS.read_text()
    assert "useSyncExternalStore" in text, "the selection is no longer a shared store"
    # Comments stripped first. A naive substring scan over this file punishes the
    # paragraph that explains the bug — which is the same trap §77's chart-token
    # test fell into, where documenting a token counted as using one.
    assert "useState" not in _without_comments(text), (
        "the account selection is back in per-component state; every caller of "
        "useAccounts() would get its own copy and only one of them would update"
    )


def test_writing_the_selection_notifies_everyone_reading_it():
    """`localStorage.setItem` fires no event in the tab that made the write —
    `storage` is for the OTHER tabs. Without the explicit notify the store is
    shared and still silent, which looks exactly like the bug it replaced."""
    text = ACCOUNT_TS.read_text()
    write = text[text.index("function store("):]
    write = write[: write.index("\n}\n") + 2]
    assert "notify()" in write, "store() writes localStorage and tells no one"

