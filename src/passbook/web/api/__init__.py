"""JSON API. SPEC §16.1.

Every route delegates to `passbook.service` — the same code `passbook sync`
runs. There is no parsing here, no push logic, no categorisation. The Phase 7
rule survives the rewrite intact: **this is a front end over service.py, never a
second implementation.**

Three constraints shape every response:

* **D5/D10 — never guess a category.** Unknown tokens are surfaced and asked
  about. `/api/categories` returns only categories that already have a rule;
  writing an unlisted one is refused with the list of known ones.
* **§6.7 — the account assertion applies here.** A statement from another
  account is refused at upload, before anything can be pushed, and the staged
  file is deleted rather than left where `make sync` would find it.
* **§11 — no secrets cross this boundary.** Account numbers are masked to last
  4 by `StatementMeta.masked_account`. The database password, the DB password, the
  customer ID and the TOTP secret never appear in a response body or a log
  line. The one exception is the TOTP secret at the moment of enrolment, which
  is the entire point of that request and is returned exactly once.

**Money is serialised as a decimal string, never a JSON number.** A JSON number
is an IEEE double the moment it is parsed, and CLAUDE.md's first non-negotiable
does not stop at the process boundary. The client formats the string without
ever converting it.
"""

# SPEC §107. One blueprint, ten modules. Importing them here is what registers
# the routes — a module nobody imports is a route that quietly does not exist,
# so `test_routes` asserts the whole table rather than trusting this list.
from ._base import MAX_UPLOAD_BYTES, api, close_clients  # noqa: F401
from . import (  # noqa: F401,E402
    accounts,
    ledger,
    ops,
    payees,
    reapply,
    registering,
    signin,
    statements,
)

# Re-exported so a caller outside this package can reach them by name without
# knowing which module they live in.
from ._base import LedgerError, open_ledger, service  # noqa: F401,E402
from ._scope import ALL_ACCOUNTS, _account_scope  # noqa: F401,E402

# **A package attribute is not a patch seam.** `monkeypatch.setattr` here cannot
# reach the submodule global a route actually resolves, so a test that wants a
# fake ledger patches `_base.open_ledger` — which is the only place a store is
# ever opened, and is the reason there is a single place to name.
__all__ = ["api", "MAX_UPLOAD_BYTES", "close_clients"]
