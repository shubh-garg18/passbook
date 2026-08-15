# Screenshots

**Everything in this directory is generated from `tests/fixtures/statement.xls`.**
Never from a real ledger. CLAUDE.md non-negotiable 14.

`docs/shots/` — where `scripts/shoot.py` writes by default — is gitignored,
because on a working install it renders real payees, real balances and a real
masked account number. This directory is the tracked one, and the only path into
it is:

```bash
make web-build                                  # a typecheck is not a build
uv run python scripts/demo_ledger.py            # a scratch Firefly, fixture rows only

# then, with the values that script prints:
FIREFLY_URL=http://localhost:8097 \
FIREFLY_TOKEN=<the minted token> \
PASSBOOK_ASSET_ACCOUNT='Demo savings account' \
PASSBOOK_ACCOUNT_NUMBER=999900001111 \
  uv run --with playwright --with pyotp python scripts/shoot.py demo

cp docs/shots/demo/<the ones you want>.png docs/screenshots/
uv run python scripts/demo_ledger.py --down     # remove the scratch stack
```

The demo's **categories are invented** and assigned round-robin over the payee
tokens. They are not a claim about what any token means — D10 forbids inferring
meaning from a truncated token, and a demo that broke the project's own rule
would be a poor advertisement for it. They exist so the charts have more than
one bar.

Before committing a screenshot, look at it. The point of a screenshot in this
project is that somebody looked.
