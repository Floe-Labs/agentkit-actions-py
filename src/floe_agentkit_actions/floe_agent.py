"""FloeAgent — runtime client for agents that hold no wallet, no private key,
no chain knowledge.

Authenticates with a ``floe_*`` agent runtime key and speaks only HTTP to the
Floe credit API. The agent's wallet (a non-custodial Privy wallet provisioned
at registration time) signs everything server-side.

What this client covers: the agent's *runtime* loop — paying for x402 APIs
and reading credit / loan state. Anything that needs management auth
(registering a new agent, opening a credit line, rotating keys) belongs in
the ``floe-agent`` CLI or the dashboard, both of which run with the
developer's Privy wallet so the dev never types a private key either.

If you need code-level access to management operations or self-custody
borrow flows, use the lower-level ``FloeActionProvider`` /
``X402ActionProvider`` with a wallet provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

import urllib.request
import urllib.error

DEFAULT_BASE_URL = "https://credit-api.floelabs.xyz"
DEFAULT_TIMEOUT_SECONDS = 15.0


@dataclass
class X402FetchResult:
    status: int
    headers: dict[str, str]
    body: str
    cost_raw: Optional[str] = None
    """USDC paid for this call (raw 6-decimal units, integer string).
    None for free passthrough responses or when the facilitator did not set
    the X-Floe-Cost-USDC header."""
    idempotent_replay: bool = False
    """True when this was a cached replay against the same idempotency key."""


@dataclass
class BalanceResult:
    credit_limit_raw: str
    credit_used_raw: str
    credit_available_raw: str
    pending_settlements_raw: str
    active_loans: list[dict[str, Any]] = field(default_factory=list)
    delegation_active: bool = False


@dataclass
class TransactionsResult:
    transactions: list[dict[str, Any]]
    next_cursor: Optional[int]
    has_more: bool


class FloeAgentError(Exception):
    """Raised when a Floe API call returns a non-2xx response.

    The ``status`` and ``code`` attributes let agent code branch on specific
    failure modes (``funding_in_progress``, ``credit_frozen``,
    ``insufficient_balance``, ``account_closed``) without string-matching
    prose. ``detail`` carries the raw response body when JSON parsing fails.
    """

    def __init__(
        self,
        message: str,
        status: int,
        code: Optional[str] = None,
        detail: Any = None,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


class FloeAgent:
    """Runtime client for x402-paying agents.

    Example::

        from floe_agentkit_actions import FloeAgent
        agent = FloeAgent(api_key=os.environ["FLOE_AGENT_API_KEY"])
        result = agent.x402_fetch(url="https://api.example.com/data")
        print(result.body)
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        if not api_key or not api_key.startswith("floe_"):
            raise ValueError(
                "FloeAgent: api_key must be a `floe_…` runtime key "
                "(mint one with `floe-agent register`)."
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    # ── public API ───────────────────────────────────────────────────────

    def x402_fetch(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[dict[str, str]] = None,
        body: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> X402FetchResult:
        """Fetch any URL through the Floe x402 facilitator.

        If the URL returns HTTP 402, the facilitator pays automatically from
        the agent's credit line and retries; the agent code sees the final
        2xx (or a FloeAgentError if credit is unavailable). Free URLs pass
        through unchanged.
        """
        extra: dict[str, str] = {}
        if idempotency_key:
            extra["Idempotency-Key"] = idempotency_key

        status, response_headers, raw_body = self._request(
            "POST",
            "/v1/proxy/fetch",
            body={"url": url, "method": method, "headers": headers, "body": body},
            extra_headers=extra,
        )

        if status >= 400:
            self._raise_from_response(status, raw_body, "x402_fetch")

        return X402FetchResult(
            status=status,
            headers=response_headers,
            body=raw_body,
            cost_raw=response_headers.get("x-floe-cost-usdc"),
            idempotent_replay=response_headers.get("x-floe-idempotent-replay") == "true",
        )

    def get_balance(self) -> BalanceResult:
        """Check the agent's credit limit, used, available, and active loans."""
        data = self._json_request("GET", "/v1/agents/balance", operation="get_balance")
        return BalanceResult(
            credit_limit_raw=data["creditLimit"],
            credit_used_raw=data["creditUsed"],
            credit_available_raw=data["creditAvailable"],
            pending_settlements_raw=data.get("pendingSettlements", "0"),
            active_loans=data.get("activeLoans", []),
            delegation_active=data.get("delegationActive", False),
        )

    def get_transactions(
        self,
        limit: int = 20,
        cursor: Optional[int] = None,
    ) -> TransactionsResult:
        """Paginated x402 payment history for this agent."""
        from urllib.parse import urlencode

        params = {"limit": str(limit)}
        if cursor is not None:
            params["cursor"] = str(cursor)
        path = f"/v1/agents/transactions?{urlencode(params)}"
        data = self._json_request("GET", path, operation="get_transactions")
        return TransactionsResult(
            transactions=data.get("transactions", []),
            next_cursor=data.get("nextCursor"),
            has_more=data.get("hasMore", False),
        )

    def estimate_x402_cost(self, url: str, method: str = "GET") -> dict[str, Any]:
        """Preview an x402 call without paying.

        Returns ``{cost_raw, will_exceed_available, x402}``. Cheap, idempotent,
        doesn't reserve balance. Use before x402_fetch when you want to make
        spend/no-spend decisions.
        """
        from urllib.parse import urlencode

        path = f"/v1/x402/estimate?{urlencode({'url': url, 'method': method})}"
        return self._json_request("GET", path, operation="estimate_x402_cost")

    # ── internals ────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        extra_headers: Optional[dict[str, str]] = None,
    ) -> tuple[int, dict[str, str], str]:
        url = f"{self._base_url}{path}"
        encoded_body = None
        if body is not None:
            encoded_body = json.dumps(body).encode("utf-8")

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, data=encoded_body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout_seconds) as resp:
                response_body = resp.read().decode("utf-8")
                response_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, response_headers, response_body
        except urllib.error.HTTPError as e:
            response_body = e.read().decode("utf-8") if e.fp else ""
            response_headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
            return e.code, response_headers, response_body
        except urllib.error.URLError as e:
            raise FloeAgentError(f"Network error reaching {url}: {e.reason}", 0) from e
        except TimeoutError as e:
            raise FloeAgentError(
                f"Request timed out after {self._timeout_seconds}s", 408
            ) from e

    def _json_request(self, method: str, path: str, operation: str) -> dict[str, Any]:
        status, _headers, body = self._request(method, path)
        if status >= 400:
            self._raise_from_response(status, body, operation)
        return json.loads(body)

    @staticmethod
    def _raise_from_response(status: int, body: str, operation: str) -> None:
        try:
            parsed = json.loads(body)
            error_code = parsed.get("error")
            detail_text = parsed.get("detail")
        except (json.JSONDecodeError, ValueError):
            parsed = body
            error_code = None
            detail_text = None
        message = detail_text or error_code or f"{operation} failed: {status}"
        raise FloeAgentError(message, status, error_code, parsed)
