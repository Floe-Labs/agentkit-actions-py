"""budget advisory on FetchResult, attribution
tags on fetch(), and report_outcome().

Mirrors the TS SDK suites (budgetAdvisory.test.ts / actionAttribution.test.ts)
using the same urlopen-stub pattern as test_floe_agent.py.
"""

from __future__ import annotations

import json
from email.message import Message
from typing import Any, Optional
from unittest.mock import patch

import pytest

from floe_agentkit_actions import FloeAgent, FloeAgentError, OutcomeResult


def _msg(headers: dict[str, str]) -> Message:
    m = Message()
    for k, v in headers.items():
        m[k] = v
    return m


class _StubResponse:
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


def _capture_urlopen(*, status: int = 200, body: bytes, headers: Optional[dict[str, str]] = None):
    """urlopen replacement that records the outbound urllib Request."""
    captured: list[Any] = []

    def _urlopen(req: Any, timeout: float = 0):  # noqa: ARG001
        captured.append(req)
        return _StubResponse(status, body, headers)

    return _urlopen, captured


ADVISORY = {
    "near_limit": True,
    "tightest": {
        "scope": "task",
        "match": "task-123",
        "used_bps": 8200,
        "remaining_raw": "450000",
        "window_kind": "rolling",
    },
}


# ── budget advisory on FetchResult ──────────────────────────────


def test_fetch_parses_budget_advisory_header() -> None:
    urlopen, _ = _capture_urlopen(
        body=b"{}",
        headers={
            "Content-Type": "application/json",
            "X-Floe-Cost-USDC": "10000",
            "X-Floe-Budget-Advisory": json.dumps(ADVISORY),
        },
    )
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", urlopen):
        result = agent.fetch(url="https://api.example.com/data")
    assert result.budget_advisory == ADVISORY
    assert result.budget_advisory["tightest"]["used_bps"] == 8200
    # Sibling fields unchanged.
    assert result.cost == pytest.approx(0.01)


def test_fetch_budget_advisory_absent_or_malformed_is_none() -> None:
    agent = FloeAgent(api_key="floe_test")
    urlopen, _ = _capture_urlopen(body=b"{}")
    with patch("urllib.request.urlopen", urlopen):
        assert agent.fetch(url="https://x.example").budget_advisory is None

    urlopen, _ = _capture_urlopen(
        body=b"{}", headers={"Content-Type": "application/json", "X-Floe-Budget-Advisory": "{"}
    )
    with patch("urllib.request.urlopen", urlopen):
        result = agent.fetch(url="https://x.example")
    assert result.budget_advisory is None
    assert result.status == 200  # malformed header never breaks the fetch


# ── attribution tags on fetch() ─────────────────────────────────


def test_fetch_sends_attribution_headers() -> None:
    urlopen, captured = _capture_urlopen(body=b"{}")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", urlopen):
        agent.fetch(url="https://api.example.com", task_id="batch-7", action_id=" Summarize-Doc-42 ")
    req = captured[0]
    # urllib capitalizes header names internally; get_header normalizes.
    assert req.get_header("X-floe-task-id") == "batch-7"
    assert req.get_header("X-floe-action-id") == "Summarize-Doc-42"  # stripped, not lowercased client-side


def test_fetch_omits_tags_when_not_given() -> None:
    urlopen, captured = _capture_urlopen(body=b"{}")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", urlopen):
        agent.fetch(url="https://api.example.com")
    req = captured[0]
    assert req.get_header("X-floe-action-id") is None
    assert req.get_header("X-floe-task-id") is None


def test_fetch_rejects_overlong_action_id_locally() -> None:
    agent = FloeAgent(api_key="floe_test")
    with pytest.raises(FloeAgentError):
        agent.fetch(url="https://api.example.com", action_id="x" * 129)


# ── report_outcome() ────────────────────────────────────────────


def test_report_outcome_posts_and_parses() -> None:
    response = {
        "actionId": "summarize-doc-42",
        "outcome": {
            "status": "success",
            "scoreBps": 9000,
            "note": None,
            "reportCount": 2,
            "reportedAt": "2026-07-15T00:00:00Z",
        },
    }
    urlopen, captured = _capture_urlopen(body=json.dumps(response).encode())
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", urlopen):
        result = agent.report_outcome("summarize-doc-42", "success", score_bps=9000)

    req = captured[0]
    assert req.full_url.endswith("/v1/agents/actions/summarize-doc-42/outcome")
    assert req.get_method() == "POST"
    assert json.loads(req.data.decode()) == {"status": "success", "scoreBps": 9000}

    assert isinstance(result, OutcomeResult)
    assert result.action_id == "summarize-doc-42"
    assert result.status == "success"
    assert result.score_bps == 9000
    assert result.report_count == 2


def test_report_outcome_validates_score_locally() -> None:
    agent = FloeAgent(api_key="floe_test")
    with pytest.raises(FloeAgentError, match="score_bps"):
        agent.report_outcome("a1", "success", score_bps=20000)


def test_report_outcome_surfaces_server_errors() -> None:
    urlopen, _ = _capture_urlopen(status=400, body=b'{"error":"invalid_action_id"}')
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", urlopen):
        with pytest.raises(FloeAgentError) as exc:
            agent.report_outcome("a1", "failure")
    assert exc.value.status == 400
    assert exc.value.code == "invalid_action_id"
