"""Schema migrations, discovered by `passbook.migrate`.

One module per migration, named `mNNN_<slug>.py`, exposing:

    VERSION      int, unique and increasing
    NAME         short kebab-case identifier, shown in `make upgrade` output
    DESCRIPTION  one sentence: what changes, and what the operator will see

    pending(ctx) -> str | None   why this needs to run, read from the LEDGER
    run(ctx)     -> None         do it
    verify(ctx)  -> str | None   what is still wrong, or None if it worked

`ctx` is a `passbook.migrate` context carrying `settings`, `client`, `registry`
and a `say(message)` for progress. See `docs/adding-a-bank.md`'s sibling,
`docs/upgrading.md`.
"""
