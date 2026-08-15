# Licences

`passbook` is licensed under the **GNU Affero General Public License, version 3
or later** (AGPL-3.0-or-later). The full text is in [`LICENSE`](LICENSE).

Copyright © 2026 the passbook contributors.

Why AGPL, and the Firefly III question, are in [SPEC §22.3](SPEC.md).

---

## Firefly III

passbook talks to a **separate, unmodified** Firefly III instance over its
documented HTTP REST API. No Firefly source is copied, vendored, patched or
linked; `docker-compose.yml` pins the official image
`fireflyiii/core:version-6.6.6` and pulls it at run time.

Firefly III is itself AGPL-3.0. Its obligations are its own — see SPEC §22.3
for why they do not reach this code, and for the one change that would make them
reach it.

## Redistributed fonts

`frontend/src/fonts/` contains **subsets** of three font families, cut down to
Latin, digits and the marks this UI actually renders. All three are under the
**SIL Open Font License 1.1**, which permits modification and redistribution
(including subsetting) provided the licence travels with the font and the
Reserved Font Names are respected.

| Family | Files | Copyright |
|---|---|---|
| Anek Latin | `AnekLatin-Display.subset.woff2`, `AnekLatin-Label.subset.woff2` | Copyright 2021 The Anek Project Authors (https://github.com/EkType/Anek) |
| Mukta | `Mukta-Regular.subset.woff2`, `Mukta-SemiBold.subset.woff2` | Copyright (c) 2014, Girish Dalvi, Ek Type. All rights reserved. |
| IBM Plex Mono | `IBMPlexMono-Regular.subset.woff2`, `IBMPlexMono-Medium.subset.woff2` | Copyright © 2017 IBM Corp. with Reserved Font Name "Plex" |

The OFL text is in [`frontend/src/fonts/OFL.txt`](frontend/src/fonts/OFL.txt).
The subsets keep the original family names, which the OFL allows; the "Reserved
Font Name" clause forbids *renaming* a derivative to the reserved name, not
subsetting under it.

## Runtime and build dependencies

Every dependency is under a licence the FSF lists as GPL-compatible, so none of
them constrains the AGPL choice. Read from the installed distributions'
own metadata rather than from memory:

| Package | Licence |
|---|---|
| `xlrd`, `httpx`, `segno`, `flask`, `werkzeug`, `jinja2`, `click`, `lxml`, `xlwt` | BSD |
| `pydantic`, `pydantic-settings`, `typer`, `pyyaml`, `ruamel.yaml`, `pyotp`, `pdfplumber`, `pdfminer.six`, `pytest` | MIT |
| `pillow` | MIT-CMU |
| `cryptography` | Apache-2.0 OR BSD-3-Clause |
| `waitress` | Zope Public License 2.1 — the FSF lists ZPL 2.0/2.1 as *"a lax, permissive non-copyleft free software license which is compatible with the GNU GPL"* |
| `pikepdf` | MPL-2.0 — compatible via its own §3.3, which the FSF describes as providing *"indirect compatibility between this license and … the GNU AGPL version 3"* |
| React, React DOM, React Router, TanStack Query, Vite, TypeScript | MIT (build-time and bundled) |

Docker images pulled at run time (`postgres:16-alpine`, `caddy:2.8-alpine`,
`fireflyiii/core`) are separate programs under their own licences and are not
redistributed here.
