"""JSON API.

Every route delegates to `passbook.service` — the same code the CLI runs. There
is no parsing here, no push logic, no categorisation: this is a front end over
the service layer, never a second implementation.

**Money is serialised as a decimal string, never a JSON number.** A JSON number
is an IEEE double the moment it is parsed, and the first non-negotiable does not
stop at the process boundary.
"""

# One blueprint, eleven modules. Importing them here is what registers the
# routes — a module nobody imports is a route that quietly does not exist.
from ._base import MAX_UPLOAD_BYTES, api  # noqa: F401
from . import (  # noqa: F401,E402
    accounts,
    banks,
    ledger,
    ops,
    payees,
    reapply,
    registering,
    signin,
    statements,
)
from ._base import FireflyClient, FireflyError, service  # noqa: F401,E402
from ._scope import ALL_ACCOUNTS, _account_scope  # noqa: F401,E402

__all__ = ["api", "MAX_UPLOAD_BYTES"]
