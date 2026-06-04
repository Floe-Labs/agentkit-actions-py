"""Tests for the FloeAgent runtime HTTP client.

These tests stub urllib so the agent never makes a real network call.
They cover:
  - Constructor input validation (api_key prefix, timeout_seconds bounds)
  - fetch() success path with the response Content-Type charset honored
  - fetch() ≥400 path with both JSON-body and non-JSON-body error decoding
  - fetch() idempotency_key client-side length validation (1..255)
  - balance() / balance_details() unit decoding
  - get_transactions() pagination
  - estimate_cost() unit decoding
  - _request error wrapping for socket-level timeouts (wrapped in URLError),
    plain URLError network errors, and OSError transport failures
  - _json_request rejects malformed 2xx bodies as FloeAgentError
"""

from __future__ import annotations

import io
import json
from email.message import Message
from typing import Any, Optional
from unittest.mock import patch

import pytest

from floe_agentkit_actions import (
    BalanceResult,
    FetchResult,
    FloeAgent,
    FloeAgentError,
    ReservationStatus,
    TransactionsResult,
)

# ── Stubs ─────────────────────────────────────────────────────────────────


def _msg(headers: dict[str, str]) -> Message:
    m = Message()
    for k, v in headers.items():
        m[k] = v
    return m


class _StubResponse:
    """Stand-in for the object returned by urllib.request.urlopen."""

    def __init__(self, status: int, body: bytes, headers: Optional[dict[str, str]] = None):
        self.status = status
        self._body = body
        self.headers = _msg(headers or {"Content-Type": "application/json; charset=utf-8"})

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_StubResponse":
        return self

    def __exit__(self, *_: Any) -> None:
        pass


def _make_urlopen(*, status: int = 200, body: bytes, headers: Optional[dict[str, str]] = None):
    """Build a urlopen replacement that returns one stub response."""

    def _urlopen(_req: Any, timeout: float = 0):  # noqa: ARG001
        return _StubResponse(status, body, headers)

    return _urlopen


# ── Constructor ──────────────────────────────────────────────────────────


def test_constructor_rejects_non_floe_api_key() -> None:
    with pytest.raises(ValueError, match="floe_"):
        FloeAgent(api_key="not_a_floe_key")
    with pytest.raises(ValueError, match="floe_"):
        FloeAgent(api_key="")


def test_constructor_accepts_floe_api_key() -> None:
    agent = FloeAgent(api_key="floe_test_key")
    assert agent._api_key == "floe_test_key"


@pytest.mark.parametrize(
    "bad_timeout",
    [0, -1, -0.5, float("nan"), float("inf"), float("-inf"), True, False, "5", None],
)
def test_constructor_rejects_invalid_timeout(bad_timeout: Any) -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        FloeAgent(api_key="floe_test_key", timeout_seconds=bad_timeout)


def test_constructor_accepts_valid_timeout() -> None:
    agent = FloeAgent(api_key="floe_test_key", timeout_seconds=5.0)
    assert agent._timeout_seconds == 5.0


def test_constructor_strips_trailing_base_url_slash() -> None:
    agent = FloeAgent(api_key="floe_test_key", base_url="https://example.com/v1/")
    assert agent._base_url == "https://example.com/v1"


# ── fetch() success path ─────────────────────────────────────────────────


def test_fetch_success_returns_dollars_and_replay_flag() -> None:
    body = b'{"hello":"world"}'
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Floe-Cost-USDC": "50000",  # $0.05
        "X-Floe-Idempotent-Replay": "true",
    }
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body, headers=headers)):
        result = agent.fetch(url="https://api.example.com/data")
    assert isinstance(result, FetchResult)
    assert result.status == 200
    assert result.body == '{"hello":"world"}'
    assert result.cost == pytest.approx(0.05)
    assert result.cost_raw == "50000"
    assert result.idempotent_replay is True


def test_fetch_success_honors_response_charset() -> None:
    # ISO-8859-1 body — "café" encoded as Latin-1.
    body = "café".encode("iso-8859-1")
    headers = {"Content-Type": "text/plain; charset=iso-8859-1"}
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body, headers=headers)):
        result = agent.fetch(url="https://api.example.com/data")
    assert result.body == "café"


def test_fetch_success_replaces_invalid_bytes_instead_of_raising() -> None:
    # Non-UTF8 bytes with no charset; should not crash.
    body = b"\xff\xfe\xfd"
    headers = {"Content-Type": "application/octet-stream"}
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body, headers=headers)):
        result = agent.fetch(url="https://api.example.com/data")
    assert isinstance(result.body, str)
    # `errors="replace"` substitutes the U+FFFD replacement character.
    assert "�" in result.body


# ── fetch() error path ───────────────────────────────────────────────────


def test_fetch_raises_floe_agent_error_with_json_body() -> None:
    err_body = b'{"error":"insufficient_balance","detail":"top up first"}'
    err = _build_http_error(status=402, body=err_body, content_type="application/json")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(FloeAgentError) as exc_info:
            agent.fetch(url="https://api.example.com/data")
    assert exc_info.value.status == 402
    assert exc_info.value.code == "insufficient_balance"
    assert "top up first" in str(exc_info.value)


def test_fetch_raises_floe_agent_error_with_non_json_body() -> None:
    err = _build_http_error(status=500, body=b"<html>oh no</html>", content_type="text/html")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(FloeAgentError) as exc_info:
            agent.fetch(url="https://api.example.com/data")
    assert exc_info.value.status == 500
    assert exc_info.value.code is None


# ── fetch() idempotency-key validation ───────────────────────────────────


def test_fetch_rejects_empty_idempotency_key() -> None:
    agent = FloeAgent(api_key="floe_test")
    with pytest.raises(FloeAgentError, match="idempotency_key"):
        agent.fetch(url="https://api.example.com/data", idempotency_key="")


def test_fetch_rejects_oversize_idempotency_key() -> None:
    agent = FloeAgent(api_key="floe_test")
    with pytest.raises(FloeAgentError, match="idempotency_key"):
        agent.fetch(url="https://api.example.com/data", idempotency_key="x" * 256)


def test_fetch_accepts_max_length_idempotency_key() -> None:
    agent = FloeAgent(api_key="floe_test")
    body = b'{"ok":true}'
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        result = agent.fetch(url="https://api.example.com/data", idempotency_key="x" * 255)
    assert result.status == 200


# ── balance() / balance_details() ────────────────────────────────────────


def test_balance_returns_dollars_only() -> None:
    # FLO-567: `available` is spendable USDC (= the legacy `balance` field
    # on facilitator responses). Pre-FLO-567 this incorrectly returned
    # `creditAvailable` (borrowing headroom), which is what the reporter hit.
    body = json.dumps(
        {
            "balance": "4000000",            # $4.00 spendable
            "creditLimit": "10000000",       # $10
            "creditUsed": "4000000",         # $4
            "creditAvailable": "6000000",    # $6 headroom (NOT spendable)
            "pendingSettlements": "200000",  # $0.20
            "activeLoans": [],
            "delegationActive": True,
        }
    ).encode("utf-8")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        avail = agent.balance()
    assert avail == pytest.approx(4.00)


def test_balance_details_returns_full_shape() -> None:
    body = json.dumps(
        {
            "balance": "4000000",
            "creditLimit": "10000000",
            "creditUsed": "4000000",
            "creditAvailable": "6000000",
            "pendingSettlements": "200000",
            "activeLoans": [{"loanId": "42", "principalRaw": "5000000"}],
            "delegationActive": True,
        }
    ).encode("utf-8")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        detail = agent.balance_details()
    assert isinstance(detail, BalanceResult)
    assert detail.available == pytest.approx(4.00)
    assert detail.credit_available == pytest.approx(6.00)
    assert detail.wallet_usdc is None  # legacy payload has no walletUsdcRaw
    assert detail.pending == pytest.approx(0.20)
    assert detail.raw.credit_limit_raw == "10000000"
    assert detail.raw.active_loans == [{"loanId": "42", "principalRaw": "5000000"}]
    assert detail.raw.delegation_active is True


# ── get_transactions() ───────────────────────────────────────────────────


def test_get_transactions_returns_typed_result() -> None:
    body = json.dumps(
        {
            "transactions": [
                {
                    "targetUrl": "https://api.example.com/data",
                    "method": "GET",
                    "paymentAmountRaw": "750000",
                    "status": "success",
                    "x402TxHash": "0xabc",
                    "createdAt": "2026-04-05T00:00:00Z",
                },
            ],
            "nextCursor": 41,
            "hasMore": True,
        }
    ).encode("utf-8")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        result = agent.get_transactions(limit=10)
    assert isinstance(result, TransactionsResult)
    assert len(result.transactions) == 1
    assert result.next_cursor == 41
    assert result.has_more is True


# ── estimate_cost() ──────────────────────────────────────────────────────


def test_estimate_cost_returns_dollars() -> None:
    body = json.dumps(
        {"priceRaw": "10000", "reflection": {"willExceedAvailable": False}, "x402": True}
    ).encode("utf-8")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        out = agent.estimate_cost(url="https://api.example.com/data")
    assert out == {"cost": pytest.approx(0.01), "can_afford": True, "is_paid": True}


# ── transport-error wrapping ─────────────────────────────────────────────


def test_request_wraps_socket_timeout_as_408() -> None:
    """urllib wraps socket.timeout in URLError(reason=TimeoutError(...)) on 3.10+."""
    import urllib.error

    err = urllib.error.URLError(reason=TimeoutError("timed out"))
    agent = FloeAgent(api_key="floe_test", timeout_seconds=2.0)
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(FloeAgentError) as exc_info:
            agent.balance()
    assert exc_info.value.status == 408
    assert exc_info.value.code == "timeout"


def test_request_wraps_plain_url_error_as_network_error() -> None:
    import urllib.error

    err = urllib.error.URLError(reason="Name or service not known")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(FloeAgentError) as exc_info:
            agent.balance()
    assert exc_info.value.status == 0
    assert exc_info.value.code == "network_error"


def test_request_wraps_oserror_as_network_error() -> None:
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", side_effect=ConnectionResetError("peer reset")):
        with pytest.raises(FloeAgentError) as exc_info:
            agent.balance()
    assert exc_info.value.status == 0
    assert exc_info.value.code == "network_error"


# ── _json_request malformed-success-body branch ──────────────────────────


def test_json_request_wraps_malformed_2xx_body() -> None:
    agent = FloeAgent(api_key="floe_test")
    bad_body = b"not json at all"
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=bad_body)):
        with pytest.raises(FloeAgentError) as exc_info:
            agent.balance()
    assert exc_info.value.code == "invalid_response_body"
    assert exc_info.value.status == 200


# ── FLO-567: balance disambiguation + await_settlement ──────────────────


def _sequenced_urlopen(responses: list[tuple[int, bytes]]):
    """urlopen replacement that returns the next response in `responses` on each call."""
    it = iter(responses)

    def _urlopen(_req: Any, timeout: float = 0):  # noqa: ARG001
        try:
            status, body = next(it)
        except StopIteration:
            raise AssertionError("more urlopen calls than seeded responses")
        return _StubResponse(status, body)

    return _urlopen


def test_balance_details_prefers_explicit_raw_fields() -> None:
    # Facilitator on FLO-567 ships both legacy and explicit fields.
    body = json.dumps(
        {
            "balance": "4000000",
            "creditLimit": "10000000",
            "creditUsed": "4000000",
            "creditAvailable": "6000000",
            "spendableRaw": "4000000",
            "creditAvailableRaw": "6000000",
            "walletUsdcRaw": "123456",
            "pendingSettlementsRaw": "500000",
            "heldUnspentRaw": "0",
            "activeLoans": [],
            "delegationActive": True,
        }
    ).encode("utf-8")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        detail = agent.balance_details()
    assert detail.available == pytest.approx(4.0)
    assert detail.credit_available == pytest.approx(6.0)
    assert detail.wallet_usdc == pytest.approx(0.123456, rel=1e-5)
    assert detail.pending == pytest.approx(0.5)
    assert detail.raw.spendable_raw == "4000000"
    assert detail.raw.wallet_usdc_raw == "123456"
    assert detail.raw.held_unspent_raw == "0"


def test_balance_exposes_spendable_vs_headroom_gap() -> None:
    # The reporter's exact scenario: $5 of headroom, $0 spendable.
    body = json.dumps(
        {
            "balance": "0",
            "creditLimit": "5000000",
            "creditUsed": "0",
            "creditAvailable": "5000000",
            "spendableRaw": "0",
            "creditAvailableRaw": "5000000",
            "walletUsdcRaw": None,
            "pendingSettlementsRaw": "0",
            "heldUnspentRaw": "0",
            "activeLoans": [],
            "delegationActive": True,
        }
    ).encode("utf-8")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        avail = agent.balance()
    assert avail == 0.0


def test_await_settlement_returns_immediately_when_terminal() -> None:
    body = json.dumps({
        "nonce": "n1", "state": "settled", "terminal": True,
        "paymentAmountRaw": "1000000", "txHash": "0xabc",
        "validBefore": 0, "reservedAt": None, "sentAt": None,
        "settledAt": "2026-05-22T12:00:00Z",
    }).encode("utf-8")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        status = agent.await_settlement("n1", interval_seconds=0.01, timeout_seconds=1.0)
    assert isinstance(status, ReservationStatus)
    assert status.state == "settled"
    assert status.tx_hash == "0xabc"


def test_await_settlement_polls_until_terminal() -> None:
    pending = json.dumps({
        "nonce": "n2", "state": "pending_settlement", "terminal": False,
        "paymentAmountRaw": "1000000", "txHash": None, "validBefore": 0,
        "reservedAt": None, "sentAt": None, "settledAt": None,
    }).encode("utf-8")
    settled = json.dumps({
        "nonce": "n2", "state": "settled", "terminal": True,
        "paymentAmountRaw": "1000000", "txHash": "0xdef", "validBefore": 0,
        "reservedAt": None, "sentAt": None, "settledAt": "2026-05-22T12:00:01Z",
    }).encode("utf-8")
    agent = FloeAgent(api_key="floe_test")
    with patch(
        "urllib.request.urlopen",
        _sequenced_urlopen([(200, pending), (200, pending), (200, settled)]),
    ):
        status = agent.await_settlement("n2", interval_seconds=0.01, timeout_seconds=2.0)
    assert status.state == "settled"
    assert status.tx_hash == "0xdef"


@pytest.mark.parametrize("terminal_state", ["settled", "payment_rejected", "expired_unsettled"])
def test_await_settlement_propagates_each_terminal_state(terminal_state: str) -> None:
    body = json.dumps({
        "nonce": f"n-{terminal_state}", "state": terminal_state, "terminal": True,
        "paymentAmountRaw": "1000000",
        "txHash": "0xabc" if terminal_state == "settled" else None,
        "validBefore": 0, "reservedAt": None, "sentAt": None,
        "settledAt": "2026-05-22T12:00:00Z" if terminal_state == "settled" else None,
    }).encode("utf-8")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        status = agent.await_settlement(
            f"n-{terminal_state}", interval_seconds=0.01, timeout_seconds=1.0,
        )
    assert status.state == terminal_state
    assert status.terminal is True


def test_await_settlement_raises_408_on_timeout() -> None:
    pending = json.dumps({
        "nonce": "n3", "state": "pending_settlement", "terminal": False,
        "paymentAmountRaw": "1000000", "txHash": None, "validBefore": 0,
        "reservedAt": None, "sentAt": None, "settledAt": None,
    }).encode("utf-8")
    agent = FloeAgent(api_key="floe_test")
    # 30+ pending responses; the helper times out before exhausting them.
    seq = [(200, pending) for _ in range(30)]
    with patch("urllib.request.urlopen", _sequenced_urlopen(seq)):
        with pytest.raises(FloeAgentError) as exc_info:
            agent.await_settlement("n3", interval_seconds=0.01, timeout_seconds=0.04)
    assert exc_info.value.status == 408
    assert exc_info.value.code == "await_settlement_timeout"


def test_fetch_attaches_parsed_502_body_with_reservation_nonce() -> None:
    # The 502 ambiguous response carries reservation.nonce so callers can
    # hand it to await_settlement instead of retrying.
    err_body = json.dumps({
        "error": "upstream_paid_request_failed_ambiguous",
        "detail": "EHOSTUNREACH",
        "reservation": {"nonce": "n-bubble-1", "validBefore": 1_700_000_000},
    }).encode("utf-8")
    err = _build_http_error(status=502, body=err_body, content_type="application/json")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", side_effect=err):
        with pytest.raises(FloeAgentError) as exc_info:
            agent.fetch(url="https://upstream.example/paid")
    assert exc_info.value.status == 502
    assert exc_info.value.code == "upstream_paid_request_failed_ambiguous"
    # detail is the parsed JSON; reservation.nonce is reachable.
    detail = exc_info.value.detail
    assert isinstance(detail, dict)
    assert detail["reservation"]["nonce"] == "n-bubble-1"


# ── helpers ──────────────────────────────────────────────────────────────


def _build_http_error(*, status: int, body: bytes, content_type: str):
    """Build a urllib.error.HTTPError that the agent's HTTPError branch will catch."""
    import urllib.error

    return urllib.error.HTTPError(
        url="https://api.example.com/data",
        code=status,
        msg="Test Error",
        hdrs=_msg({"Content-Type": content_type}),
        fp=io.BytesIO(body),
    )


# ── fetch() omits None headers/body ────────────────────────────────────


def test_fetch_omits_none_headers_and_body() -> None:
    """fetch(url) must NOT send headers:null or body:null in the JSON payload."""
    captured: list[Any] = []
    original_urlopen = None

    def _capturing_urlopen(req: Any, timeout: float = 0):  # noqa: ARG001
        captured.append(json.loads(req.data))
        return _StubResponse(200, b'{"ok":true}')

    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _capturing_urlopen):
        agent.fetch("https://api.example.com/data")

    assert len(captured) == 1
    payload = captured[0]
    assert "headers" not in payload
    assert "body" not in payload
    assert payload["url"] == "https://api.example.com/data"
    assert payload["method"] == "GET"


def test_fetch_includes_headers_and_body_when_provided() -> None:
    captured: list[Any] = []

    def _capturing_urlopen(req: Any, timeout: float = 0):  # noqa: ARG001
        captured.append(json.loads(req.data))
        return _StubResponse(200, b'{"ok":true}')

    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _capturing_urlopen):
        agent.fetch("https://api.example.com/data", method="POST",
                     headers={"X-Custom": "val"}, body='{"q":"hi"}')

    payload = captured[0]
    assert payload["headers"] == {"X-Custom": "val"}
    assert payload["body"] == '{"q":"hi"}'


# ── fetch() auto-borrow retry ──────────────────────────────────────────


def test_fetch_retries_on_auto_borrow_in_progress() -> None:
    """402 auto_borrow_in_progress triggers retry; success on 2nd attempt."""
    import urllib.error

    call_count = 0

    def _sequence_urlopen(req: Any, timeout: float = 0):  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise urllib.error.HTTPError(
                url="https://api.example.com/data",
                code=402,
                msg="Payment Required",
                hdrs=_msg({"Content-Type": "application/json"}),
                fp=io.BytesIO(json.dumps({
                    "error": "auto_borrow_in_progress",
                    "retry_after_seconds": 0.01,
                }).encode()),
            )
        return _StubResponse(200, b'{"ok":true}',
                              {"Content-Type": "application/json", "X-Floe-Cost-USDC": "1000"})

    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _sequence_urlopen), \
         patch("time.sleep") as mock_sleep:
        result = agent.fetch("https://api.example.com/data")

    assert call_count == 2
    assert result.status == 200
    mock_sleep.assert_called_once()


def test_fetch_gives_up_after_max_retries() -> None:
    """After 2 auto_borrow retries, the 3rd 402 raises FloeAgentError."""
    import urllib.error

    def _always_402(req: Any, timeout: float = 0):  # noqa: ARG001
        raise urllib.error.HTTPError(
            url="https://api.example.com/data",
            code=402,
            msg="Payment Required",
            hdrs=_msg({"Content-Type": "application/json"}),
            fp=io.BytesIO(json.dumps({
                "error": "auto_borrow_in_progress",
                "retry_after_seconds": 0.01,
            }).encode()),
        )

    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _always_402), \
         patch("time.sleep"):
        with pytest.raises(FloeAgentError) as exc_info:
            agent.fetch("https://api.example.com/data")
    assert exc_info.value.status == 402


def test_fetch_no_retry_on_regular_402() -> None:
    """A non-auto_borrow 402 must NOT trigger retry."""
    import urllib.error

    call_count = 0

    def _regular_402(req: Any, timeout: float = 0):  # noqa: ARG001
        nonlocal call_count
        call_count += 1
        raise urllib.error.HTTPError(
            url="https://api.example.com/data",
            code=402,
            msg="Payment Required",
            hdrs=_msg({"Content-Type": "application/json"}),
            fp=io.BytesIO(json.dumps({
                "error": "insufficient_balance",
                "available": "0",
                "required": "1000",
            }).encode()),
        )

    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _regular_402):
        with pytest.raises(FloeAgentError):
            agent.fetch("https://api.example.com/data")
    assert call_count == 1


# ── estimate_cost ──────────────────────────────────────────────────────────
# Regression: estimate_cost previously issued GET /v1/x402/estimate and read
# top-level costRaw / willExceedAvailable. The server is POST-only and returns
# priceRaw + nested reflection.willExceedAvailable. These lock the real contract.


def _capture_urlopen(*, status: int, body: bytes, sink: dict[str, Any]):
    def _urlopen(req: Any, timeout: float = 0):  # noqa: ARG001
        sink["method"] = req.get_method()
        sink["url"] = req.full_url
        sink["data"] = req.data
        return _StubResponse(status, body)

    return _urlopen


def test_estimate_cost_posts_and_maps_real_response_shape() -> None:
    sink: dict[str, Any] = {}
    body = json.dumps({
        "x402": True,
        "priceRaw": "1500000",  # 1.5 USDC
        "reflection": {"willExceedAvailable": False},
    }).encode()
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _capture_urlopen(status=200, body=body, sink=sink)):
        out = agent.estimate_cost("https://api.vendor.test/data", "GET")

    assert sink["method"] == "POST"
    assert sink["url"].endswith("/v1/x402/estimate")
    assert json.loads(sink["data"]) == {"url": "https://api.vendor.test/data", "method": "GET"}
    assert out["cost"] == pytest.approx(1.5)
    assert out["can_afford"] is True
    assert out["is_paid"] is True


def test_estimate_cost_can_afford_false_when_reflection_exceeds() -> None:
    body = json.dumps({
        "x402": True,
        "priceRaw": "9000000",
        "reflection": {"willExceedAvailable": True},
    }).encode()
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        out = agent.estimate_cost("https://api.vendor.test/data")
    assert out["can_afford"] is False


def test_estimate_cost_non_x402_is_free_and_affordable() -> None:
    body = json.dumps({"x402": False}).encode()
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        out = agent.estimate_cost("https://example.com")
    assert out["is_paid"] is False
    assert out["cost"] == 0
    assert out["can_afford"] is True
