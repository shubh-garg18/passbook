# Security policy

passbook handles bank statements: account numbers, customer IDs, postal
addresses, counterparty phone numbers, and a complete balance history. A bug
here does not leak "data", it leaks somebody's finances. Please report privately.

## Reporting a vulnerability

**Use GitHub's private reporting** — the *Security* tab on this repository →
*Report a vulnerability*. That opens a channel only the maintainers can read.

If that is unavailable to you, open a public issue containing **only** the words
"security report, please open a private channel" and nothing else. Do not
describe the issue in it.

Please include, in the private report:

- what an attacker can reach, and what they need to start with (local shell?
  a browser on the same machine? a file the user is tricked into uploading?);
- the smallest reproduction you have;
- the version — a commit SHA, or the output of `passbook --version`.

**Redact your own data first.** A traceback carrying a real narration or account
number turns your report into a second incident. `****1111` is fine.

### What to expect

This is a single-maintainer hobby project, not a funded product.

| | |
|---|---|
| Acknowledgement | within 7 days |
| First assessment | within 14 days |
| Fix or a stated decision not to | within 90 days, or an explanation of the delay |

You will be credited in the release notes unless you would rather not be. There
is no bounty; there is no budget.

## Scope

**In scope** — anything that lets someone who should not have it reach a
statement, a ledger, a backup, or a credential:

- authentication and session handling in the web UI (`src/passbook/web/`,
  `src/passbook/webauth.py`);
- the upload path — a crafted file that escapes the parser, writes outside
  `inbox/`, or survives a failed validation;
- secrets reaching somewhere they should not: a log line, an HTTP response, a
  rendered page, a backup archive, an error message;
- the backup and disaster-recovery path (`scripts/backup_remote.sh`,
  `scripts/dr_drill.sh`) — particularly anything that would ship plaintext
  off-machine;
- the account assertion (SPEC §6.7/§21.7): a statement from an unregistered
  account importing silently.

**Out of scope**, because they are documented design decisions rather than
oversights — argue with them in an issue instead:

- **No HTTPS.** Everything binds `127.0.0.1`. The threat model is one operator
  on one machine, and a self-signed certificate on loopback buys nothing.
- **Anyone with a shell on the machine has already won.** `.env` holds the
  Firefly token and the database password; `backups/` holds plaintext financial
  history. That is stated in the README, not hidden.
- **The PDF password is not a secret.** It is the last four digits of the
  account number — the same four the UI already prints as `****1111`. RC4-40
  over a four-digit secret is ten thousand combinations against a broken
  cipher. The protection is nominal and the documentation says so.
- **Firefly III's own vulnerabilities.** Report those to
  [firefly-iii/firefly-iii](https://github.com/firefly-iii/firefly-iii).
  passbook pins an official image and calls its REST API.
- Anything requiring physical access to an unlocked machine.

## What this project does about security already

So you know what is deliberate rather than missing:

- **Everything binds `127.0.0.1`**, asserted by a test over `docker-compose.yml`
  rather than trusted to review.
- **Two factors on the web UI** — a scrypt password plus TOTP, with eight
  mandatory single-use backup codes. The session is an httpOnly,
  `SameSite=Strict` cookie, never a token in `localStorage`. Six failures in
  fifteen minutes locks the account out.
- **The unknown-username timing oracle is closed.** That path used to skip
  hashing and answered measurably faster; it now runs a full scrypt
  verification against a throwaway hash.
- **A TOTP code cannot be replayed** — the accepted counter is persisted and
  anything at or below it is refused.
- **The web container is unprivileged and has no Docker socket**, asserted at
  AST level rather than by grep. It reports backup health; the host runs
  backups. Otherwise a compromise of the one component that listens on a port
  and parses untrusted uploads would be root on the machine and the power to
  delete every backup, including the off-site copies.
- **Nothing secret reaches a template or a response.** Account numbers are
  masked to last four; the Firefly token, database password, `APP_KEY` and
  customer ID never enter a render context — including in error paths, which is
  where full numbers usually leak.
- **Off-site backups are encrypted before they leave the machine** (GPG
  AES256), and each archive is decrypted and byte-compared before upload. The
  passphrase lives outside the repo and outside `.env`.
- **Web credentials are excluded from the backup on purpose**, and the backup
  target asserts it on the finished tarball rather than trusting the glob to
  stay narrow.

## Not a vulnerability, but tell us anyway

If you find a way passbook can **silently produce a wrong ledger** — rows
dropped, a balance that reconciles with itself while being wrong, a figure that
looks plausible and is not — please report it with the same urgency. A ledger
once held 21 of 93 rows for seven hours behind an all-green status strip. On a
tool people use to understand their own money, silence is the failure mode that
matters most.
