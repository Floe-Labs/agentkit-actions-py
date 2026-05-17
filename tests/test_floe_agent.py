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
    body = json.dumps(
        {
            "creditLimit": "10000000",       # $10
            "creditUsed": "3200000",         # $3.20
            "creditAvailable": "6800000",    # $6.80
            "pendingSettlements": "200000",  # $0.20
            "activeLoans": [],
            "delegationActive": True,
        }
    ).encode("utf-8")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        avail = agent.balance()
    assert avail == pytest.approx(6.80)


def test_balance_details_returns_full_shape() -> None:
    body = json.dumps(
        {
            "creditLimit": "10000000",
            "creditUsed": "3200000",
            "creditAvailable": "6800000",
            "pendingSettlements": "200000",
            "activeLoans": [{"loanId": "42", "principalRaw": "5000000"}],
            "delegationActive": True,
        }
    ).encode("utf-8")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", _make_urlopen(status=200, body=body)):
        detail = agent.balance_details()
    assert isinstance(detail, BalanceResult)
    assert detail.available == pytest.approx(6.80)
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
        {"costRaw": "10000", "willExceedAvailable": False, "x402": True}
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
