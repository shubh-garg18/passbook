"""Firefly III REST integration. SPEC §7.

Every field name and error shape in this package was read from the running
instance (v6.6.6) rather than from memory or published docs — see the header of
`push.py` for exactly what was verified and how.
"""

from .client import (  # noqa: F401
    DuplicateTransaction,
    FireflyClient,
    FireflyError,
    ValidationFailed,
)
