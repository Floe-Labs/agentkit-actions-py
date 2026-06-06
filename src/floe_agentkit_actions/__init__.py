"""Coinbase AgentKit ActionProvider for Floe DeFi lending protocol on Base.

Usage::

    from floe_agentkit_actions import floe_action_provider

    provider = floe_action_provider()
"""

from __future__ import annotations

from typing import Any

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


# CrewAI integration symbols are re-exported lazily (PEP 562) so `crewai`
# stays an optional extra — importing this package never requires it until one
# of these names is actually accessed.
_CREWAI_EXPORTS = frozenset(
    {
        "get_floe_crewai_tools",
        "Floe402Tool",
        "FloeLLM",
        "FloeBudget",
        "budget_enabled_agent",
    }
)


def __getattr__(name: str) -> Any:
    if name in _CREWAI_EXPORTS:
        from .integrations import crewai as _crewai

        return getattr(_crewai, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "FloeActionProvider", "FloeConfig", "floe_action_provider",
    "X402ActionProvider", "X402Config", "x402_action_provider",
    # High-level runtime client (no wallet, no chain knowledge — `floe_*` key only).
    "FloeAgent", "FloeAgentError",
    "FetchResult", "BalanceResult", "RawBalance", "ReservationStatus", "TransactionsResult",
    "X402FetchResult",  # deprecated alias for FetchResult
    # CrewAI integration (optional extra — lazy).
    "get_floe_crewai_tools", "Floe402Tool", "FloeLLM", "FloeBudget", "budget_enabled_agent",
]
