"""Regression tests for X402ActionProvider x402_get_transactions tx-limit clamp.

Mirrors agentkit-actions (TypeScript) commits 40cc356 / b4bcb3c which clamp
the client-supplied limit to <=100 and default to 20 for non-numeric /
negative / zero inputs. The Python port was MISSING this fix when these
tests were added — the clamp was ported as part of the same change.

Locks down src/floe_agentkit_actions/x402_action_provider.py
x402_get_transactions (around line 467).
"""

from __future__ import annotations

from typing import Any

import pytest

from floe_agentkit_actions.x402_action_provider import X402ActionProvider, X402Config


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

    def sign_message(self, _message: str) -> str:
        return "0xsig"


def _make_provider() -> X402ActionProvider:
    return X402ActionProvider(X402Config(
        matcher_address="0x17946cD3e180f82e632805e5549EC913330Bb175",
        facilitator_url="https://x402.floe.xyz",
    ))


def _capture_fetch() -> tuple[X402ActionProvider, list[str]]:
    """Return (provider, urls_captured). Patches _facilitator_fetch to
    record every path it was called with and return an empty-tx-list
    happy-path response."""
    provider = _make_provider()
    captured: list[str] = []

    def _spy(path: str, method: str = "GET", body: Any = None) -> dict[str, Any]:
        captured.append(path)
        return {"status": 200, "body": {"transactions": [], "hasMore": False}, "headers": {}}

    provider._facilitator_fetch = _spy  # type: ignore[method-assign]
    return provider, captured


@pytest.mark.parametrize(
    "input_limit,expected_limit",
    [
        ("150", 100),    # clamped to 100
        ("10000", 100),  # clamped to 100
        ("100", 100),    # unchanged at boundary
        ("50", 50),      # passes through
        ("20", 20),      # passes through
        ("abc", 20),     # non-numeric → default 20
        ("-5", 20),      # negative → default 20
        ("0", 20),       # zero → default 20
        ("", 20),        # empty string → default 20
    ],
)
def test_limit_clamped_and_defaulted(input_limit: str, expected_limit: int) -> None:
    provider, captured = _capture_fetch()
    provider.x402_get_transactions(NoopWallet(), {"limit": input_limit})

    assert len(captured) == 1, f"expected exactly 1 facilitator call, got {len(captured)}"
    path = captured[0]
    assert f"limit={expected_limit}" in path, (
        f"expected limit={expected_limit} in URL but got: {path}"
    )
