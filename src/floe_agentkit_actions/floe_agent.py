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
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Literal, Optional, TypedDict

DEFAULT_BASE_URL = "https://credit-api.floelabs.xyz"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_IDEMPOTENCY_KEY_LENGTH = 255
MAX_TAG_LENGTH = 128
USDC_DECIMALS = 6
USDC_SCALE = 10**USDC_DECIMALS


def _decode_response(payload: bytes, headers: Any) -> str:
    """Decode an HTTP response body to text using the server-advertised charset.

    ``fetch()`` proxies arbitrary URLs, so the response may be ISO-8859-1,
    Windows-1252, raw binary, etc. Hardcoding UTF-8 raises UnicodeDecodeError
    before the caller ever gets a FetchResult or a typed FloeAgentError,
    breaking the public contract.

    Pulls the charset from Content-Type via ``HTTPMessage.get_content_charset``
    when available, falls back to UTF-8, and uses ``errors="replace"`` so a
    malformed body still surfaces as a (best-effort) string instead of
    blowing up the request.
    """
    if not payload:
        return ""
    charset: Optional[str] = None
    getter = getattr(headers, "get_content_charset", None) if headers is not None else None
    if callable(getter):
        try:
            charset = getter()
        except (LookupError, TypeError):
            charset = None
    try:
        return payload.decode(charset or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _raw_to_dollars(raw: Optional[str]) -> float:
    """Convert a raw USDC integer string (6 decimals) to a dollar float.

    Display-quality precision only. Python ints are arbitrary-precision, but
    the division produces a float — which is exact up to ``sys.float_info``'s
    safe-integer ceiling (~$9.0 × 10^9 of USDC). Real-world agent balances
    sit well below that, so this is fine for showing dollars. Settlement-
    grade code (on-chain math, accounting reconciliation) should keep using
    the raw integer strings on ``FetchResult.cost_raw`` /
    ``RawBalance.*_raw`` and never round-trip through this function.
    """
    if not raw:
        return 0.0
    try:
        return int(raw) / USDC_SCALE
    except (TypeError, ValueError):
        return 0.0


class BudgetAdvisoryTightest(TypedDict, total=False):
    """The tightest active spend cap, as reported by the server (snake_case
    wire shape verbatim — mirrors the TS SDK's ``BudgetAdvisory``)."""

    scope: str  # credit_line | session | task | api | vendor | key
    match: Optional[str]
    used_bps: int  # 0..10000
    remaining_raw: str  # raw 6-decimal USDC integer string
    window_kind: Optional[str]  # once | rolling | session | credit_line | None
    window_resets_at: str  # ISO-8601; rolling windows only


class BudgetAdvisory(TypedDict, total=False):
    """Parsed ``X-Floe-Budget-Advisory`` response header — how close the agent
    is to the tightest spend cap Floe enforces, so it can taper or escalate
    *before* the 402 hard-stop fires."""

    near_limit: bool  # present only when the operator configured a threshold
    tightest: BudgetAdvisoryTightest


def _parse_budget_advisory(headers: dict[str, str]) -> Optional[BudgetAdvisory]:
    """Defensively parse ``x-floe-budget-advisory`` — a malformed or absent
    header yields ``None``, never an exception (mirrors the TS SDK)."""
    raw = headers.get("x-floe-budget-advisory")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _validate_tag(name: str, value: str) -> str:
    """Validate a ``task_id``/``action_id`` attribution tag: 1..128 chars
    after stripping. The server lowercases; the stripped value passes through
    unchanged."""
    trimmed = value.strip()
    if len(trimmed) == 0 or len(trimmed) > MAX_TAG_LENGTH:
        raise FloeAgentError(
            f"{name} must be 1..{MAX_TAG_LENGTH} characters after stripping "
            f"(got {len(trimmed)}).",
            400,
        )
    return trimmed


@dataclass
class FetchResult:
    status: int
    headers: dict[str, str]
    body: str
    cost: float = 0.0
    """Dollar amount paid for this call (0 for free passthrough)."""
    idempotent_replay: bool = False
    """True when this was a cached replay against the same idempotency key."""
    cost_raw: Optional[str] = None
    """Raw 6-decimal USDC integer string. Advanced — prefer ``cost`` for display."""
    budget_advisory: Optional[BudgetAdvisory] = None
    """Parsed ``X-Floe-Budget-Advisory`` (proximity to the tightest spend cap).

    ``None`` when the server flag is off, the facilitator predates the
    header, or the header is malformed."""


# Backwards compatibility alias.
X402FetchResult = FetchResult


@dataclass
class OutcomeResult:
    """The stored outcome for a tagged action, as returned by
    :meth:`FloeAgent.report_outcome` (FLO-633). Caller-supplied verbatim —
    Floe never judges quality."""

    action_id: str
    status: str  # success | failure | partial | unknown
    score_bps: Optional[int] = None
    """Optional quality score, basis points 0..10000 (e.g. 8500 = 85%)."""
    note: Optional[str] = None
    report_count: int = 1
    """How many times this action's outcome has been (re)reported."""
    reported_at: Optional[str] = None


@dataclass
class RawBalance:
    credit_limit_raw: str
    credit_used_raw: str
    credit_available_raw: str
    pending_settlements_raw: str
    # FLO-567: explicit field names that disambiguate spendable USDC
    # (what the proxy gates on) from borrowing headroom (what the agent
    # could draw). ``wallet_usdc_raw`` is the on-chain USDC balance of
    # the Privy custodial wallet — ``None`` when the facilitator could
    # not read it or the field is absent (older facilitator).
    spendable_raw: str = "0"
    wallet_usdc_raw: Optional[str] = None
    held_unspent_raw: str = "0"
    active_loans: list[dict[str, Any]] = field(default_factory=list)
    delegation_active: bool = False


@dataclass
class BalanceResult:
    available: float
    """Dollar amount the agent can actually spend right now (backed by
    ``spendable_raw``). NOT the same as ``credit_available`` — an agent
    with a $100 delegation but no facility loan opened has ``available == 0``.
    """
    credit_available: float
    """Dollar amount of operator-delegation headroom (backed by
    ``credit_available_raw``) — how much more the agent could borrow."""
    wallet_usdc: Optional[float]
    """On-chain USDC balance of the Privy custodial wallet, or ``None`` when
    the facilitator could not read it."""
    pending: float
    """Dollar amount currently reserved against in-flight payments."""
    raw: RawBalance
    """Raw integer-string fields for advanced callers."""


# FLO-567 reservation-status return type for ``await_settlement``.
@dataclass
class ReservationStatus:
    nonce: str
    state: str
    """One of: reserved | sent | pending_settlement | settled |
    expired_unsettled | payment_rejected."""
    terminal: bool
    payment_amount_raw: str
    tx_hash: Optional[str]
    valid_before: int
    reserved_at: Optional[str]
    sent_at: Optional[str]
    settled_at: Optional[str]


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
        # Reject 0, negative, NaN, +/-Infinity — urllib treats these
        # inconsistently (0 = no timeout on some platforms; NaN raises a
        # TypeError deep inside socket code). Fail fast at construction.
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError(
                f"FloeAgent: timeout_seconds must be a finite positive number "
                f"(got {timeout_seconds!r})."
            )
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = float(timeout_seconds)

    # ── public API ───────────────────────────────────────────────────────

    def fetch(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[dict[str, str]] = None,
        body: Optional[str] = None,
        idempotency_key: Optional[str] = None,
        task_id: Optional[str] = None,
        action_id: Optional[str] = None,
    ) -> FetchResult:
        """Call any URL.

        If the API is x402-gated, payment happens automatically (debited
        from your prepaid balance). Free URLs pass through unchanged.

        ``task_id`` (≤128 chars, lowercased server-side) is sent as
        ``X-Floe-Task-Id`` — spend accrues against any per-task budget with
        that id. ``action_id`` is sent as ``X-Floe-Action-Id`` — it attributes
        this call's cost to one decision/action of your run so it can be
        joined against the outcome you later report via
        :meth:`report_outcome` (cost-per-action eval). Both ride on the Floe
        request itself, not on the target's headers.
        """
        extra: dict[str, str] = {}
        if idempotency_key is not None:
            if (
                len(idempotency_key) == 0
                or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH
            ):
                raise FloeAgentError(
                    f"idempotency_key must be 1..{MAX_IDEMPOTENCY_KEY_LENGTH} "
                    f"characters (got {len(idempotency_key)}).",
                    400,
                )
            extra["Idempotency-Key"] = idempotency_key
        if task_id is not None:
            extra["X-Floe-Task-Id"] = _validate_tag("task_id", task_id)
        if action_id is not None:
            extra["X-Floe-Action-Id"] = _validate_tag("action_id", action_id)

        payload: dict[str, Any] = {"url": url, "method": method}
        if headers is not None:
            payload["headers"] = headers
        if body is not None:
            payload["body"] = body

        max_auto_borrow_retries = 2
        for attempt in range(max_auto_borrow_retries + 1):
            status, response_headers, raw_body = self._request(
                "POST",
                "/v1/proxy/fetch",
                body=payload,
                extra_headers=extra,
            )

            # Auto-borrow retry: server is topping up the credit line.
            # Wait and retry before giving up.
            if status == 402 and attempt < max_auto_borrow_retries:
                try:
                    err_body = json.loads(raw_body)
                    if (
                        isinstance(err_body, dict)
                        and err_body.get("error") == "auto_borrow_in_progress"
                    ):
                        raw_delay = err_body.get("retry_after_seconds", 10)
                        delay = min(max(float(raw_delay), 1), 60)
                        time.sleep(delay)
                        continue
                except (ValueError, TypeError):
                    pass
            break

        if status >= 400:
            self._raise_from_response(status, raw_body, "fetch")

        cost_raw = response_headers.get("x-floe-cost-usdc")
        return FetchResult(
            status=status,
            headers=response_headers,
            body=raw_body,
            cost=_raw_to_dollars(cost_raw),
            cost_raw=cost_raw,
            idempotent_replay=response_headers.get("x-floe-idempotent-replay") == "true",
            budget_advisory=_parse_budget_advisory(response_headers),
        )

    # Backwards compatibility alias — `fetch` is the recommended name.
    x402_fetch = fetch

    def report_outcome(
        self,
        action_id: str,
        status: Literal["success", "failure", "partial", "unknown"],
        score_bps: Optional[int] = None,
        note: Optional[str] = None,
    ) -> OutcomeResult:
        """Report how a tagged action turned out, closing the spend ↔ outcome
        loop (FLO-633). Pair with ``fetch(..., action_id=...)``::

            agent.fetch(url, action_id="summarize-doc-42")
            agent.report_outcome("summarize-doc-42", "success", score_bps=9000)

        Your operator's dashboard then shows cost-per-action next to your
        outcome. Re-reporting the same action replaces the previous signal
        (``report_count`` increments). Floe never judges quality — the
        status/score are yours, stored verbatim.
        """
        tag = _validate_tag("action_id", action_id)
        if score_bps is not None and (
            not isinstance(score_bps, int)
            or isinstance(score_bps, bool)
            or score_bps < 0
            or score_bps > 10000
        ):
            raise FloeAgentError(
                f"score_bps must be an integer 0..10000 (got {score_bps}).", 400
            )
        payload: dict[str, Any] = {"status": status}
        if score_bps is not None:
            payload["scoreBps"] = score_bps
        if note is not None:
            payload["note"] = note
        data = self._json_request(
            "POST",
            f"/v1/agents/actions/{urllib.parse.quote(tag, safe='')}/outcome",
            operation="report_outcome",
            body=payload,
        )
        outcome = data.get("outcome") or {}
        return OutcomeResult(
            action_id=str(data.get("actionId", tag)),
            status=str(outcome.get("status", status)),
            score_bps=outcome.get("scoreBps"),
            note=outcome.get("note"),
            report_count=int(outcome.get("reportCount", 1)),
            reported_at=outcome.get("reportedAt"),
        )

    def balance(self) -> float:
        """Return the agent's spendable balance in dollars.

        For most code this is the one number you want::

            if agent.balance() < 5:
                top_up()

        For full detail (pending settlements, raw integer units, active
        working-capital loans), call ``balance_details()``.
        """
        return self.balance_details().available

    def balance_details(self) -> BalanceResult:
        """Full balance breakdown including pending settlements and raw values.

        FLO-567: ``available`` now reads ``spendableRaw`` (what the proxy
        gates on), falling back to the legacy ``balance`` field for older
        facilitators. ``credit_available`` is exposed separately as
        borrowing headroom.
        """
        data = self._json_request("GET", "/v1/agents/balance", operation="balance")
        # Prefer FLO-567 explicit names; fall back to legacy. `or` is
        # deliberate (not Pythonic `if x is not None else …`): the only
        # legitimate falsy value the server emits is the string "0", which
        # is truthy in Python, so `"0" or fallback` correctly yields "0".
        # An empty string would (incorrectly) fall through — but the API
        # never returns one, so the simpler form stays safe.
        spendable_raw = data.get("spendableRaw") or data.get("balance") or "0"
        credit_available_raw = data.get("creditAvailableRaw") or data["creditAvailable"]
        pending_raw = (
            data.get("pendingSettlementsRaw") or data.get("pendingSettlements") or "0"
        )
        held_raw = data.get("heldUnspentRaw") or "0"
        wallet_usdc_raw = data.get("walletUsdcRaw")  # may be None or absent
        return BalanceResult(
            available=_raw_to_dollars(spendable_raw),
            credit_available=_raw_to_dollars(credit_available_raw),
            wallet_usdc=(
                _raw_to_dollars(wallet_usdc_raw) if wallet_usdc_raw is not None else None
            ),
            pending=_raw_to_dollars(pending_raw),
            raw=RawBalance(
                credit_limit_raw=data["creditLimit"],
                credit_used_raw=data["creditUsed"],
                credit_available_raw=credit_available_raw,
                pending_settlements_raw=pending_raw,
                spendable_raw=spendable_raw,
                wallet_usdc_raw=wallet_usdc_raw,
                held_unspent_raw=held_raw,
                active_loans=data.get("activeLoans", []),
                delegation_active=data.get("delegationActive", False),
            ),
        )

    # Backwards compatibility alias.
    get_balance = balance_details

    def await_settlement(
        self,
        nonce: str,
        interval_seconds: float = 2.0,
        timeout_seconds: float = 15 * 60.0,
    ) -> ReservationStatus:
        """Poll until an x402 reservation reaches a terminal state.

        Use this after ``fetch`` raises ``FloeAgentError`` with code
        ``"upstream_paid_request_failed_ambiguous"`` — the raised error's
        ``detail["reservation"]["nonce"]`` is what to pass in here. Do NOT
        retry the original ``fetch`` call; it may double-charge.

        Resolves with the final ``ReservationStatus`` once ``state`` is one of
        ``settled``, ``payment_rejected``, or ``expired_unsettled``. Raises
        ``FloeAgentError`` with status 408 on timeout, or 404 if the nonce is
        not owned by this agent.
        """
        import time as _time
        from urllib.parse import quote as _quote

        if interval_seconds <= 0:
            raise ValueError("await_settlement: interval_seconds must be > 0")
        if timeout_seconds <= 0:
            raise ValueError("await_settlement: timeout_seconds must be > 0")

        deadline = _time.monotonic() + timeout_seconds
        path = f"/v1/agents/reservations/{_quote(nonce, safe='')}"
        last_state: Optional[str] = None
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise FloeAgentError(
                    f"await_settlement: timed out after {timeout_seconds}s "
                    f"(last state: {last_state})",
                    408,
                    "await_settlement_timeout",
                )
            per_call_timeout = min(self._timeout_seconds, remaining)
            data = self._json_request(
                "GET", path, operation="await_settlement", timeout=per_call_timeout
            )
            last_state = data.get("state")
            if data.get("terminal"):
                return ReservationStatus(
                    nonce=data["nonce"],
                    state=data["state"],
                    terminal=True,
                    payment_amount_raw=data.get("paymentAmountRaw", "0"),
                    tx_hash=data.get("txHash"),
                    valid_before=int(data.get("validBefore", 0)),
                    reserved_at=data.get("reservedAt"),
                    sent_at=data.get("sentAt"),
                    settled_at=data.get("settledAt"),
                )
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise FloeAgentError(
                    f"await_settlement: timed out after {timeout_seconds}s "
                    f"(last state: {last_state})",
                    408,
                    "await_settlement_timeout",
                    data,
                )
            _time.sleep(min(interval_seconds, remaining))

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

    def estimate_cost(self, url: str, method: str = "GET") -> dict[str, Any]:
        """Preview the cost of calling a URL without actually paying.

        Returns ``{"cost": float, "can_afford": bool, "is_paid": bool}``.
        Cheap, idempotent, doesn't reserve balance.
        """
        raw = self._json_request(
            "POST",
            "/v1/x402/estimate",
            operation="estimate_cost",
            body={"url": url, "method": method},
        )
        reflection = raw.get("reflection") or {}
        return {
            "cost": _raw_to_dollars(raw.get("priceRaw")),
            "can_afford": not reflection.get("willExceedAvailable", False),
            "is_paid": bool(raw.get("x402", False)),
        }

    # Backwards compatibility alias.
    estimate_x402_cost = estimate_cost

    # ── internals ────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        path: str,
        body: Any = None,
        extra_headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
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

        effective_timeout = self._timeout_seconds if timeout is None else timeout
        req = urllib.request.Request(url, data=encoded_body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                response_body = _decode_response(resp.read(), resp.headers)
                response_headers = {k.lower(): v for k, v in resp.headers.items()}
                return resp.status, response_headers, response_body
        except urllib.error.HTTPError as e:
            response_body = _decode_response(e.read(), e.headers) if e.fp else ""
            response_headers = {k.lower(): v for k, v in e.headers.items()} if e.headers else {}
            return e.code, response_headers, response_body
        except urllib.error.URLError as e:
            # On Python 3.10+, socket.timeout is an alias of TimeoutError, and
            # urllib wraps it in URLError(reason=TimeoutError(...)). Detect that
            # wrapped form here so callers see a typed 408, not a generic
            # "network error".
            if isinstance(e.reason, TimeoutError):
                raise FloeAgentError(
                    f"Request timed out after {effective_timeout}s",
                    408,
                    "timeout",
                ) from e
            raise FloeAgentError(
                f"Network error reaching {url}: {e.reason}",
                0,
                "network_error",
            ) from e
        except OSError as e:
            # Lower-level socket / DNS errors that don't get wrapped in URLError
            # (ConnectionResetError, certain SSL errors, etc.). Surface as a
            # typed error so callers can branch on FloeAgentError uniformly.
            raise FloeAgentError(
                f"Network error reaching {url}: {e}", 0, "network_error"
            ) from e

    def _json_request(
        self,
        method: str,
        path: str,
        operation: str,
        body: Any = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        status, _headers, raw = self._request(method, path, body=body, timeout=timeout)
        if status >= 400:
            self._raise_from_response(status, raw, operation)
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as e:
            raise FloeAgentError(
                f"{operation} returned {status} but the body was not valid JSON.",
                status,
                "invalid_response_body",
                raw,
            ) from e

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
