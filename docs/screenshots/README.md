# Screenshots

[← README](../../README.md) · [Setup](../SETUP.md) · [Usage](../usage.md) ·
[Backups](../backups.md)

**Everything in this directory is generated from `tests/fixtures/statement.xls`.**
Never from a real ledger. DECISIONS.md §40, non-negotiable 16.

`docs/shots/` — where `scripts/shoot.py` writes by default — is gitignored,
because on a working install it renders real payees, real balances and a real
masked account number. This directory is the tracked one, and the only path into
it is:

```bash
make web-build                                  # a typecheck is not a build
uv run --with playwright --with pyotp python scripts/demo_ledger.py --shoot demo
cp docs/shots/demo/<the ones you want>.png docs/screenshots/
uv run python scripts/demo_ledger.py --down     # remove the scratch stack
```

One command. It stands up a scratch database on its own docker network, writes
the 93 fixture rows into it, photographs every page, and hands back the PNGs.
`shoot.py` runs from a temporary directory whose only archived statement is the
fixture, so it cannot read a real `archive/` even on a working install.

The demo's **categories are invented** and assigned round-robin over the payee
tokens. They are not a claim about what any token means — D10 forbids inferring
meaning from a truncated token, and a demo that broke the project's own rule
would be a poor advertisement for it. They exist so the charts have more than
one bar.

Before committing a screenshot, look at it. The point of a screenshot in this
project is that somebody looked.
