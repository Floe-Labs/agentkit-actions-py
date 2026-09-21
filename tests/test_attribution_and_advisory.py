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

from floe_agentkit_actions import (
    FloeAgent,
    FloeAgentError,
    OutcomeClaim,
    OutcomeResult,
)


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


# ── emit_outcome() ──────────────────────────────────────────────


def test_emit_outcome_posts_and_parses() -> None:
    response = {
        "outcome": {
            "eventId": "oev_00112233445566aa",
            "interactionId": "int_00112233445566bb",
            "outcomeKind": "meeting_booked",
            "status": "reported",
            "quantity": 1,
            "occurredAt": "2026-09-15T00:00:00Z",
            "confirmedAt": None,
            "source": "agent",
            "externalSystem": None,
            "externalRef": None,
            "evidenceNote": None,
            "supersedesEventId": None,
            "billedInPeriodId": None,
        }
    }
    urlopen, captured = _capture_urlopen(body=json.dumps(response).encode())
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", urlopen):
        claim = agent.emit_outcome(
            "call-8821",
            "meeting_booked",
            idempotency_key="call-8821:meeting_booked",
        )

    req = captured[0]
    assert req.full_url.endswith("/v1/agents/outcomes")
    assert req.get_method() == "POST"
    # Omitted optionals stay ABSENT, not null — the route is strict.
    assert json.loads(req.data.decode()) == {
        "taskId": "call-8821",
        "outcomeKind": "meeting_booked",
        "idempotencyKey": "call-8821:meeting_booked",
    }
    assert isinstance(claim, OutcomeClaim)
    assert claim.event_id == "oev_00112233445566aa"
    assert claim.status == "reported"
    # Emitting never sets the billing anchor; only an operator's confirm does.
    assert claim.confirmed_at is None


def test_emit_outcome_sends_evidence_allowlist() -> None:
    response = {
        "outcome": {
            "eventId": "oev_00112233445566aa",
            "interactionId": "int_1",
            "outcomeKind": "meeting_booked",
            "status": "reported",
            "quantity": 2,
            "occurredAt": "2026-09-15T00:00:00Z",
            "confirmedAt": None,
            "source": "agent",
            "externalSystem": "hubspot",
            "externalRef": "DEAL-9",
            "evidenceNote": None,
            "supersedesEventId": None,
            "billedInPeriodId": None,
        }
    }
    urlopen, captured = _capture_urlopen(body=json.dumps(response).encode())
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", urlopen):
        claim = agent.emit_outcome(
            "call-8821",
            "meeting_booked",
            idempotency_key="k1",
            quantity=2,
            external_system="hubspot",
            external_ref="DEAL-9",
        )

    sent = json.loads(captured[0].data.decode())
    assert sent["quantity"] == 2
    # Verbatim: a CRM id is case-sensitive, and equality on this pair is what
    # proves two claims are one fact.
    assert sent["externalRef"] == "DEAL-9"
    assert claim.external_ref == "DEAL-9"


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-date",
        "2026-09-15",                # date only — the route refuses it
        "2026-09-15T10:30:00+01:00",  # offset — the route demands Z
        "2026-13-45T00:00:00Z",      # shape-valid, not a real instant
    ],
)
def test_emit_outcome_rejects_a_non_iso_occurred_at_locally(bad: str) -> None:
    """The route declares occurredAt as z.string().datetime(); a value that is
    merely a string round-trips to a 400 the SDK could have named itself."""
    urlopen, captured = _capture_urlopen(body=b"{}")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", urlopen):
        with pytest.raises(FloeAgentError, match="occurred_at"):
            agent.emit_outcome(
                "call-1", "meeting_booked", idempotency_key="k1", occurred_at=bad
            )
    assert captured == []  # refused before any request


def test_emit_outcome_sends_a_well_formed_occurred_at() -> None:
    response = {
        "outcome": {
            "eventId": "oev_00112233445566aa",
            "interactionId": "int_1",
            "outcomeKind": "meeting_booked",
            "status": "reported",
            "quantity": 1,
            "occurredAt": "2026-09-15T10:30:00Z",
            "confirmedAt": None,
            "source": "agent",
            "externalSystem": None,
            "externalRef": None,
            "evidenceNote": None,
            "supersedesEventId": None,
            "billedInPeriodId": None,
        }
    }
    urlopen, captured = _capture_urlopen(body=json.dumps(response).encode())
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", urlopen):
        agent.emit_outcome(
            "call-1",
            "meeting_booked",
            idempotency_key="k1",
            occurred_at="2026-09-15T10:30:00Z",
        )

    assert json.loads(captured[0].data.decode())["occurredAt"] == "2026-09-15T10:30:00Z"


@pytest.mark.parametrize("bad", [123, True, None.__class__, object()])
def test_emit_outcome_rejects_a_non_string_task_id_locally(bad: object) -> None:
    """`_validate_tag` called `.strip()` on whatever it was given, so a
    non-string raised AttributeError instead of the typed error this client
    promises for every other bad argument. Hardened in the helper, which
    covers fetch's two tags and report_outcome as well."""
    urlopen, captured = _capture_urlopen(body=b"{}")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", urlopen):
        with pytest.raises(FloeAgentError, match="task_id must be a string"):
            agent.emit_outcome(bad, "meeting_booked", idempotency_key="k1")  # type: ignore[arg-type]
    assert captured == []  # refused before any request


def test_report_outcome_also_rejects_a_non_string_action_id() -> None:
    """The same hardening, reached through the other caller — proof it was
    fixed in the helper rather than patched at one call site."""
    urlopen, captured = _capture_urlopen(body=b"{}")
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", urlopen):
        with pytest.raises(FloeAgentError, match="action_id must be a string"):
            agent.report_outcome(42, "success")  # type: ignore[arg-type]
    assert captured == []


def test_emit_outcome_rejects_external_ref_without_system_locally() -> None:
    agent = FloeAgent(api_key="floe_test")
    with pytest.raises(FloeAgentError, match="external_ref requires external_system"):
        agent.emit_outcome(
            "call-1", "meeting_booked", idempotency_key="k1", external_ref="DEAL-9"
        )


def test_emit_outcome_validates_kind_and_quantity_locally() -> None:
    agent = FloeAgent(api_key="floe_test")
    with pytest.raises(FloeAgentError, match="outcome_kind"):
        agent.emit_outcome("call-1", "x" * 65, idempotency_key="k1")
    with pytest.raises(FloeAgentError, match="quantity"):
        agent.emit_outcome(
            "call-1", "meeting_booked", idempotency_key="k1", quantity=0
        )


def test_emit_outcome_allows_a_key_longer_than_a_tag_but_caps_at_200() -> None:
    """The idempotency key is not an attribution tag: rejecting one the API
    would have accepted is a client bug, not strictness."""
    agent = FloeAgent(api_key="floe_test")
    with pytest.raises(FloeAgentError, match="idempotency_key"):
        agent.emit_outcome("call-1", "meeting_booked", idempotency_key="k" * 201)


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


def test_report_outcome_rejects_bool_score() -> None:
    # bool subclasses int in Python — True must not serialize as scoreBps.
    agent = FloeAgent(api_key="floe_test")
    with pytest.raises(FloeAgentError, match="score_bps"):
        agent.report_outcome("a1", "success", score_bps=True)  # type: ignore[arg-type]


def test_report_outcome_surfaces_server_errors() -> None:
    urlopen, _ = _capture_urlopen(status=400, body=b'{"error":"invalid_action_id"}')
    agent = FloeAgent(api_key="floe_test")
    with patch("urllib.request.urlopen", urlopen):
        with pytest.raises(FloeAgentError) as exc:
            agent.report_outcome("a1", "failure")
    assert exc.value.status == 400
    assert exc.value.code == "invalid_action_id"
