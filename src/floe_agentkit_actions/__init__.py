"""Coinbase AgentKit ActionProvider for Floe DeFi lending protocol on Base.

Usage::

    from floe_agentkit_actions import floe_action_provider

    provider = floe_action_provider()
"""

from __future__ import annotations

from .action_provider import FloeActionProvider
from .types import FloeConfig


def floe_action_provider(config: FloeConfig | None = None) -> FloeActionProvider:
    """Create a new FloeActionProvider instance.

    Args:
        config: Optional configuration with custom contract addresses and market IDs.

    Returns:
        A FloeActionProvider ready to register with AgentKit.
    """
    return FloeActionProvider(config)


__all__ = ["FloeActionProvider", "FloeConfig", "floe_action_provider"]
