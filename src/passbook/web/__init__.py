"""Local web front end. SPEC §16.

A JSON API over `passbook.service` plus the React bundle that consumes it.
An additional front end, never a second implementation: there is one parser,
one push path and one balance invariant, and the CLI still runs all of them.
"""

from .app import create_app  # noqa: F401
