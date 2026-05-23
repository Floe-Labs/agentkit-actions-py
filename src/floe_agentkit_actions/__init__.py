"""Coinbase AgentKit ActionProvider for Floe DeFi lending protocol on Base.

Usage::

    from floe_agentkit_actions import floe_action_provider

    provider = floe_action_provider()
"""

from __future__ import annotations

from .action_provider import FloeActionProvider
from .floe_agent import (
    BalanceResult,
    FetchResult,
    FloeAgent,
    FloeAgentError,
    RawBalance,
    ReservationStatus,
    TransactionsResult,
    X402FetchResult,  # deprecated alias for FetchResult
)
from .types import FloeConfig
from .x402_action_provider import X402ActionProvider, X402Config, x402_action_provider


def floe_action_provider(config: FloeConfig | None = None) -> FloeActionProvider:
    """Create a new FloeActionProvider instance.

    Args:
        config: Optional configuration with custom contract addresses and market IDs.

    Returns:
        A FloeActionProvider ready to register with AgentKit.
    """
    return FloeActionProvider(config)


__all__ = [
    "FloeActionProvider", "FloeConfig", "floe_action_provider",
    "X402ActionProvider", "X402Config", "x402_action_provider",
    # High-level runtime client (no wallet, no chain knowledge — `floe_*` key only).
    "FloeAgent", "FloeAgentError",
    "FetchResult", "BalanceResult", "RawBalance", "ReservationStatus", "TransactionsResult",
    "X402FetchResult",  # deprecated alias for FetchResult
]
