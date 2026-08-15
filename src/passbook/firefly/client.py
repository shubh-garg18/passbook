"""Thin httpx wrapper around the Firefly III API. SPEC §7.1.

Bearer auth, `Accept: application/vnd.api+json`, retry with backoff on 5xx, and
a typed error on 4xx that surfaces the response body — Firefly's validation
errors are detailed and worth reading.

The token is never logged, never included in an exception message, and never
placed in a URL. SPEC §11.
"""

import json
import logging
import time

import httpx

log = logging.getLogger(__name__)

# Verified on the running instance: a duplicate is reported as HTTP 422 with the
# factory's message text, not a distinct status code. See push.py.
DUPLICATE_MARKER = "duplicate of transaction"


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
                last = FireflyError(f"cannot reach Firefly at {self.base_url}: {exc}")
                log.debug("attempt %d/%d failed: %s", attempt, self.retries, exc)
            else:
                if response.status_code < 400:
                    if not response.content:
                        return {}
                    return response.json()
                body = _safe_json(response)
                if response.status_code >= 500:
                    last = FireflyError(
                        f"Firefly returned {response.status_code}", response.status_code, body
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
        return self._request("GET", "/api/v1/about").get("data", {})

    def asset_accounts(self) -> list[dict]:
        page, out = 1, []
        while True:
            data = self._request(
                "GET", "/api/v1/accounts", params={"type": "asset", "page": page}
            )
            out.extend(data.get("data", []))
            meta = (data.get("meta") or {}).get("pagination") or {}
            if page >= int(meta.get("total_pages", 1)):
                return out
            page += 1

    def store_transaction(self, payload: dict) -> dict:
        """POST /api/v1/transactions. Raises DuplicateTransaction on a dup."""
        return self._request("POST", "/api/v1/transactions", content=json.dumps(payload))

    def _paged(self, path: str, **params) -> list[dict]:
        page, out = 1, []
        while True:
            data = self._request("GET", path, params={**params, "page": page, "limit": 50})
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
        """Every transaction group touching one account, following pagination."""
        page, out = 1, []
        while True:
            data = self._request(
                "GET",
                f"/api/v1/accounts/{account_id}/transactions",
                params={"page": page, "limit": 50},
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
        self._request("DELETE", f"/api/v1/transactions/{group_id}")

    def purge_trashed(self) -> None:
        """DELETE /api/v1/data/purge — force-delete every soft-deleted record.

        Required after deleting transactions, not optional. Firefly's
        duplicate-hash check (`TransactionJournalFactory::errorIfDuplicate`)
        queries `withTrashed()`, so a soft-deleted row keeps blocking
        re-insertion of identical content forever. `PurgeController@purge`
        force-deletes `onlyTrashed()` journals and groups, which clears it.
        """
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
            "Firefly rejected the token (401). It may have expired — a Personal "
            "Access Token lasts 365 days. Run `passbook doctor`.",
            status,
            body,
        )
    return FireflyError(f"Firefly returned {status}: {message}", status, body)
