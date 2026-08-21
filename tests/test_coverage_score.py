"""Regression tests for X402ActionProvider get_coverage_score.

The Coverage Score reports the share of an agent's known spend that Floe
enforces pre-call vs reconciled (off-path) vs dark. Kept at behavioral parity
with the TypeScript port (agentkit-actions, floe-agent).

Locks down src/floe_agentkit_actions/x402_action_provider.py get_coverage_score
and the GetCoverageScoreSchema `days` window (default 30, 1..365).
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from floe_agentkit_actions.x402_action_provider import (
    GetCoverageScoreSchema,
    X402ActionProvider,
    X402Config,
)


class NoopWallet:
    def get_address(self) -> str:
        return "0x1111111111111111111111111111111111111111"

    def get_name(self) -> str:
        return "noop-wallet"

    def get_network(self) -> Any:
        from unittest.mock import MagicMock

        net = MagicMock()
        net.chain_id = "8453"
        net.network_id = "base-mainnet"
        net.protocol_family = "evm"
        return net


def _make_provider() -> X402ActionProvider:
    return X402ActionProvider(X402Config(
        matcher_address="0x17946cD3e180f82e632805e5549EC913330Bb175",
        facilitator_url="https://credit-api.floelabs.xyz",
    ))


_HAPPY_BODY = {
    "coverageScoreBps": 6200,
    "days": 30,
    "preCallEnforceableBps": 6200,
    "reconciledBps": 2500,
    "darkBps": 1300,
}


def _capture_fetch(body: dict[str, Any], status: int = 200) -> tuple[X402ActionProvider, list[str]]:
    provider = _make_provider()
    captured: list[str] = []

    def _spy(path: str, method: str = "GET", body_arg: Any = None) -> dict[str, Any]:
        captured.append(path)
        return {"status": status, "body": body, "headers": {}}

    provider._facilitator_fetch = _spy  # type: ignore[method-assign]
    return provider, captured


def test_default_days_window_hits_versioned_path() -> None:
    provider, captured = _capture_fetch(_HAPPY_BODY)
    provider.get_coverage_score(NoopWallet(), {"days": 30})

    assert len(captured) == 1
    assert captured[0] == "/v1/agents/coverage?days=30"


def test_custom_days_window_forwarded() -> None:
    provider, captured = _capture_fetch(_HAPPY_BODY)
    provider.get_coverage_score(NoopWallet(), {"days": 7})

    assert captured[0] == "/v1/agents/coverage?days=7"


def test_missing_days_defaults_to_30() -> None:
    provider, captured = _capture_fetch(_HAPPY_BODY)
    provider.get_coverage_score(NoopWallet(), {})

    assert captured[0] == "/v1/agents/coverage?days=30"


def test_output_renders_score_and_breakdown() -> None:
    provider, _ = _capture_fetch(_HAPPY_BODY)
    out = provider.get_coverage_score(NoopWallet(), {"days": 30})

    assert "## Coverage Score" in out
    assert "**Coverage**: 62.00% (last 30 days)" in out
    assert "**Pre-call enforceable**: 62.00%" in out
    assert "**Reconciled (off-path)**: 25.00%" in out
    assert "**Dark**: 13.00%" in out
    assert "budget measure, not a wallet balance" in out


def test_error_status_surfaces_message() -> None:
    provider, _ = _capture_fetch({"error": "agent not found"}, status=404)
    out = provider.get_coverage_score(NoopWallet(), {"days": 30})

    assert out.startswith("Error:")
    assert "agent not found" in out


@pytest.mark.parametrize("days", [0, -1, 366])
def test_schema_rejects_out_of_range_window(days: int) -> None:
    with pytest.raises(ValidationError):
        GetCoverageScoreSchema(days=days)


def test_schema_defaults_days_to_30() -> None:
    assert GetCoverageScoreSchema().days == 30
