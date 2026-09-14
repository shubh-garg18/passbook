"""Thin httpx wrapper around the Firefly III API. SPEC §7.1.

Bearer auth, `Accept: application/vnd.api+json`, retry with backoff on 5xx, and
a typed error on 4xx that surfaces the response body — Firefly's validation
errors are detailed and worth reading.

The token is never logged, never included in an exception message, and never
placed in a URL. SPEC §11.
"""

import hashlib
import json
import logging
import time
from contextlib import contextmanager

import httpx

log = logging.getLogger(__name__)

# Verified on the running instance: a duplicate is reported as HTTP 422 with the
# factory's message text, not a distinct status code. See push.py.
DUPLICATE_MARKER = "duplicate of transaction"

# Firefly pages at 50 by default and validates `limit` as `min:1|max:131337`
# (`Requests/PaginationRequest::rules()` on the pinned tag). Measured on a
# 114-row account: three pages at 50 cost 735ms, one page at 200+ cost 380ms.
# The dominant cost of every read in this app is the round trip, not the rows.
#
# 500 rather than the maximum: enough for years of a personal account in one
# request, small enough that a response stays a sane size, and the pagination
# loop below is unchanged — so a ledger larger than this still works, just in
# more than one trip.
PAGE_SIZE = 500


#: Answers from the store that outlive one request. SPEC §101.
#:
#: **Measured before it was written**, per §6k. Timed from inside the store's own
#: container, so this is the store answering, not the network:
#:
#:     GET /api/v1/about                       109ms
#:     GET /api/v1/accounts?type=asset         220ms   (two accounts)
#:     GET /api/v1/accounts/3/transactions     359ms   (114 groups)
#:
#: A single Ledger page load asks three endpoints at once and every one of them
#: needs the account list; two of them need the transactions as well. So ~85% of
#: the wall clock was the store re-answering questions whose answers had not
#: changed since the last one, a second earlier.
#:
#: Two rules keep this from becoming a lie:
#:
#: * **every write through this client clears it.** A push, an update, a delete,
#:   a purge and an account creation all invalidate, so the operator never sees
#:   their own change arrive late;
#: * **`fresh()` bypasses it**, and `verify_ledger` uses that. Non-negotiable 11:
#:   a green tick for something you did not check is a lie, and a check that
#:   compares `archive/` against a cached view of the ledger has not checked the
#:   ledger.
#:
#: What is left is a write by another process — `passbook push` in a terminal —
#: which this one cannot see. The TTL is the bound on that, and it is why there
#: is one at all rather than a cache invalidated purely by writes.
_SHARED: dict[tuple, tuple[float, object]] = {}

#: Seconds. Long enough to collapse one page load and the navigation after it,
#: short enough that a write this process did not make shows up on its own.
SHARED_TTL = 30.0


def forget_everything() -> None:
    """Drop the shared cache. For tests, and for a caller that knows better."""
    _SHARED.clear()


class FireflyError(Exception):
    """Any non-success response. Carries the status and the parsed body."""

    def __init__(self, message: str, status: int | None = None, body=None) -> None:
        super().__init__(message)
        self.status = status
        self.body = body


class ValidationFailed(FireflyError):
    """422. Firefly rejected the payload; `errors` says why, per field."""

    @property
    def errors(self) -> dict:
        if isinstance(self.body, dict):
            return self.body.get("errors") or {}
        return {}


class DuplicateTransaction(ValidationFailed):
    """422 whose message is Firefly's duplicate-hash rejection.

    A normal outcome on overlapping weekly downloads, not an error. SPEC §7.2.
    """


class FireflyClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 30.0,
        retries: int = 3,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        # Per-client memo; see `asset_accounts`. A client is one request long.
        self._asset_accounts: list[dict] | None = None
        # Part of every shared-cache key: two tokens on one URL are two users'
        # data. Hashed, so a key is never a credential — not in a traceback, not
        # in a repr (non-negotiable 4). Truncated because it identifies rather
        # than authenticates.
        self._token_digest = hashlib.sha256((token or "").encode()).hexdigest()[:16]
        self._skip_cache = False
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.api+json",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FireflyClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- plumbing -------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        last: Exception | None = None

        for attempt in range(1, self.retries + 1):
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.RequestError as exc:
                # Connection-level failure: worth retrying, and the message
                # carries no credentials.
                last = FireflyError(f"cannot reach the ledger store at {self.base_url}: {exc}")
                log.debug("attempt %d/%d failed: %s", attempt, self.retries, exc)
            else:
                if response.status_code < 400:
                    if not response.content:
                        return {}
                    return response.json()
                body = _safe_json(response)
                if response.status_code >= 500:
                    last = FireflyError(
                        f"the ledger store returned {response.status_code}",
                        response.status_code,
                        body,
                    )
                    log.debug("attempt %d/%d got %d", attempt, self.retries, response.status_code)
                else:
                    raise _client_error(response.status_code, body)

            if attempt < self.retries:
                time.sleep(0.5 * 2 ** (attempt - 1))

        assert last is not None
        raise last

    # --- endpoints ------------------------------------------------------------

    def about(self) -> dict:
        """The store's version and driver. Cached (§101): it changes when the
        container restarts, and the strip asks for it on every page load."""
        return self._cached("about", self._fetch_about)

    def _fetch_about(self) -> dict:
        return self._request("GET", "/api/v1/about").get("data", {})

    @contextmanager
    def fresh(self):
        """Read past the shared cache for the duration of this block. §101.

        For a caller whose whole job is to say what the ledger actually holds —
        `verify_ledger`, and anything that reports rather than renders. A check
        that reads a cached view has not checked anything (non-negotiable 11).

        Reads inside the block still *fill* the cache, so the fresh answer is
        the one everyone else gets afterwards.
        """
        was, self._skip_cache = self._skip_cache, True
        try:
            yield self
        finally:
            self._skip_cache = was

    def _cache_key(self, what: str) -> tuple:
        # The token is part of the key because two tokens on one URL are two
        # different users' data. Hashed, so a cache key is never a credential
        # even in a traceback (non-negotiable 4).
        return (self.base_url, self._token_digest, what)

    def _cached(self, what: str, fetch):
        key = self._cache_key(what)
        if not self._skip_cache:
            entry = _SHARED.get(key)
            if entry is not None and time.monotonic() - entry[0] < SHARED_TTL:
                return entry[1]
        value = fetch()
        _SHARED[key] = (time.monotonic(), value)
        return value

    def _invalidate(self) -> None:
        """Everything this client could have cached. §101.

        The whole entry set for this base URL, not the one key that obviously
        moved: storing a transaction changes an account's transaction list *and*
        its balance, and guessing which keys a write touches is how a cache
        starts lying. Clearing a handful of entries costs one round trip.
        """
        prefix = (self.base_url, self._token_digest)
        for key in [k for k in _SHARED if k[:2] == prefix]:
            del _SHARED[key]

    def asset_accounts(self) -> list[dict]:
        """Every asset account, cached. SPEC §101.

        Memoised per client since §51 — several endpoints ask for it two or
        three times inside one request, resolving an account id and then
        checking a name is free. That memo is still here and is still the first
        thing consulted; the shared cache underneath it is what stops the *next*
        request paying 220ms for the same two rows.
        """
        if self._asset_accounts is None:
            self._asset_accounts = self._cached("asset_accounts", self._fetch_asset_accounts)
        return self._asset_accounts

    def _fetch_asset_accounts(self) -> list[dict]:
        page, out = 1, []
        while True:
            data = self._request(
                "GET",
                "/api/v1/accounts",
                params={"type": "asset", "page": page, "limit": PAGE_SIZE},
            )
            out.extend(data.get("data", []))
            meta = (data.get("meta") or {}).get("pagination") or {}
            if page >= int(meta.get("total_pages", 1)):
                return out
            page += 1

    def store_account(self, payload: dict) -> dict:
        """POST /api/v1/accounts. SPEC §26.7.

        Read off the pinned tag (v6.6.6), not remembered —
        `Requests/Models/Account/StoreRequest::rules()`:

          * `name` is required and carries `uniqueAccountForUser`, so creating
            one that already exists is a 422 rather than a silent second
            account with the same name.
          * `type` must be a key of `config('firefly.subTitlesByIdentifier')`:
            asset / expense / revenue / cash / liabilities / liability.
          * **`account_role` is `required_if:type,asset`** — an asset account
            without one is rejected. Valid roles come from
            `config('firefly.accountRoles')`: defaultAsset, sharedAsset,
            savingAsset, ccAsset, cashWalletAsset.
          * `currency_code` is `min:3|max:3|exists:transaction_currencies,code`.
        """
        created = self._request("POST", "/api/v1/accounts", content=json.dumps(payload))
        self._asset_accounts = None  # the memo is now stale within this request
        self._invalidate()  # and so is the shared one (§101)
        return created

    def store_transaction(self, payload: dict) -> dict:
        """POST /api/v1/transactions. Raises DuplicateTransaction on a dup."""
        self._invalidate()  # §101
        return self._request("POST", "/api/v1/transactions", content=json.dumps(payload))

    def update_transaction(self, group_id: str | int, payload: dict) -> dict:
        """PUT /api/v1/transactions/{group}. A **sparse** update. SPEC §23.

        Read off the pinned tag (v6.6.6), not remembered:
        `routes/api.php` binds `Route::put('{transactionGroup}', UpdateController@update)`
        under the `v1/transactions` prefix, and
        `Requests/Models/Transaction/UpdateRequest::getTransactionData()` builds
        each split from an empty array, copying only the keys the request
        actually carries. Fields left out are left alone — which is what makes
        renaming a payee safe: `amount`, `date` and `type` are never sent.
        """
        self._invalidate()  # §101
        return self._request(
            "PUT", f"/api/v1/transactions/{group_id}", content=json.dumps(payload)
        )

    def _paged(self, path: str, **params) -> list[dict]:
        page, out = 1, []
        while True:
            data = self._request(
                "GET", path, params={**params, "page": page, "limit": PAGE_SIZE}
            )
            out.extend(data.get("data", []))
            meta = (data.get("meta") or {}).get("pagination") or {}
            if page >= int(meta.get("total_pages", 1)):
                return out
            page += 1

    def rule_groups(self) -> list[dict]:
        return self._paged("/api/v1/rule-groups")

    def rules(self) -> list[dict]:
        return self._paged("/api/v1/rules")

    def store_rule_group(self, payload: dict) -> dict:
        return self._request("POST", "/api/v1/rule-groups", content=json.dumps(payload))

    def update_rule(self, rule_id: str | int, payload: dict) -> dict:
        return self._request("PUT", f"/api/v1/rules/{rule_id}", content=json.dumps(payload))

    def store_rule(self, payload: dict) -> dict:
        return self._request("POST", "/api/v1/rules", content=json.dumps(payload))

    def categories(self) -> list[dict]:
        return self._paged("/api/v1/categories")

    def account_transactions(self, account_id: str | int) -> list[dict]:
        """Every transaction group touching one account, following pagination.

        Cached (§101). The returned list is **shared** — callers read it or
        build from it, and none of them may mutate it in place.
        """
        return self._cached(
            f"account_transactions:{account_id}",
            lambda: self._fetch_account_transactions(account_id),
        )

    def _fetch_account_transactions(self, account_id: str | int) -> list[dict]:
        page, out = 1, []
        while True:
            data = self._request(
                "GET",
                f"/api/v1/accounts/{account_id}/transactions",
                params={"page": page, "limit": PAGE_SIZE},
            )
            out.extend(data.get("data", []))
            meta = (data.get("meta") or {}).get("pagination") or {}
            if page >= int(meta.get("total_pages", 1)):
                return out
            page += 1

    def delete_transaction(self, group_id: str | int) -> None:
        """DELETE /api/v1/transactions/{group}. Returns 204 with no body.

        Firefly answers **401, not 404**, for a group that is absent or not
        yours — it declines to leak existence. Callers must not read a 401 here
        as an auth failure without checking; see purge.py.
        """
        self._invalidate()  # §101
        self._request("DELETE", f"/api/v1/transactions/{group_id}")

    def purge_trashed(self) -> None:
        """DELETE /api/v1/data/purge — force-delete every soft-deleted record.

        Required after deleting transactions, not optional. Firefly's
        duplicate-hash check (`TransactionJournalFactory::errorIfDuplicate`)
        queries `withTrashed()`, so a soft-deleted row keeps blocking
        re-insertion of identical content forever. `PurgeController@purge`
        force-deletes `onlyTrashed()` journals and groups, which clears it.
        """
        self._invalidate()  # §101
        self._request("DELETE", "/api/v1/data/purge")

    def token_is_valid(self) -> bool:
        """Cheap probe used to tell 'group is gone' from 'token died'."""
        try:
            self.about()
            return True
        except FireflyError:
            return False


def _safe_json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return response.text[:2000]


def _client_error(status: int, body) -> FireflyError:
    message = body.get("message") if isinstance(body, dict) else str(body)

    if status == 422:
        # A duplicate and a genuine validation failure BOTH arrive as 422 keyed
        # on `transactions.0.description` — the key alone cannot tell them
        # apart, so match the message. Verified on the instance: an empty POST
        # yields "Need at least one transaction." under that same key.
        errors = (body or {}).get("errors", {}) if isinstance(body, dict) else {}
        haystack = " ".join(
            [str(message or "")] + [str(v) for values in errors.values() for v in values]
        ).lower()
        if DUPLICATE_MARKER in haystack:
            return DuplicateTransaction(str(message), status, body)
        return ValidationFailed(str(message), status, body)

    if status == 401:
        return FireflyError(
            "The ledger store rejected the token (401). It may have expired — a "
            "Personal Access Token lasts 365 days. Status shows how long is left.",
            status,
            body,
        )
    return FireflyError(f"the ledger store returned {status}: {message}", status, body)
