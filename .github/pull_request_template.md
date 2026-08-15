## What this changes

<!-- One or two sentences. -->

## What you measured

<!--
The useful sentence. Numbers, not adjectives:

  "Tested against my HDFC savings export, 214 rows, 0 continuity breaks,
   computed final balance equal to the closing sentinel."
  "Reintroduced the bug and watched the new test go red."
  "Screenshotted both themes at 1440px and 390px; the category column no
   longer clips."

If you could not verify something, say so here rather than leaving it implied.
-->

## Checklist

- [ ] `make test` passes
- [ ] `make audit-docs` passes — no real balance, payee, account number or UTR
      in any tracked file
- [ ] No statement file, real screenshot or backup is in this PR
- [ ] No check was loosened to make a test pass (see CONTRIBUTING.md)
- [ ] If this changes the shape of stored data, it ships a migration under
      `src/passbook/migrations/` (see `docs/upgrading.md`)
- [ ] If this changes anything rendered, I ran `make web-build` and then
      `scripts/shoot.py`, and **looked at the PNGs**

## For a new bank

- [ ] `src/passbook/banks/<bank>.py`
- [ ] A redacted fixture, with the `--audit` output quoted below
- [ ] Tests: parse, every narration grammar, a deleted row failing the
      continuity check, and `detect` claiming my fixture but not Canara's
- [ ] The module docstring records anything the bank does that surprised me
