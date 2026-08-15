/* The state a restore guarantees. SPEC §16.9, §16.11.
 *
 * `config/web-auth.json` is deliberately excluded from the backup, so a
 * recovered install has no credentials at all. Before this page existed the
 * operator met an ordinary sign-in form that rejected everything with
 * "Sign-in failed." — indistinguishable from a typo, with the real reason
 * visible only in `docker compose logs web`. The DR drill walked into exactly
 * that and it is what this replaces.
 *
 * Saying so leaks nothing: there is no account here to enumerate.
 */

import { Notice } from '../components/ui'

export function NotConfigured() {
  return (
    <div className="page page--narrow">
      <h1>Not set up yet</h1>
      <p className="lede">
        This install has no web credentials. Nothing is wrong — it is what a fresh install
        and a restored one both look like.
      </p>

      <Notice>
        <p>
          On the host, in the repository:
        </p>
        <pre className="diff">
          <span>make web-password</span>
        </pre>
        <p>
          It prompts for a username and password twice, writes{' '}
          <code>config/web-auth.json</code> with mode 600, and stores no plaintext. No
          restart is needed — this page re-reads the file on every request.
        </p>
        <p>Then reload here. You will be asked to enrol an authenticator and shown eight
          backup codes once.</p>
      </Notice>

      <h2>If you are recovering from a backup</h2>
      <p className="muted">
        This is step 7 of the disaster-recovery runbook, and it is expected. The password,
        the TOTP secret and the backup codes are deliberately <strong>not</strong> carried
        in the off-site archive: they hold no information you cannot recreate in two
        minutes, and archiving them would put a live second factor next to the password
        hash it exists to be independent of.
      </p>
      <p className="muted">
        Your ledger is unaffected. Transactions, balances, categories, aliases and rules all
        come back from the dump and the config tarball.
      </p>
    </div>
  )
}
