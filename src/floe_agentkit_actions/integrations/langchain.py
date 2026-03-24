"""LangChain integration helper."""

from __future__ import annotations

from typing import Any

from ..types import FloeConfig


def get_floe_langchain_tools(
    wallet_provider: Any,
    config: FloeConfig | None = None,
) -> list[Any]:
    """Create LangChain tools from Floe AgentKit actions.

    Requires: pip install floe-agentkit-actions[langchain]

    Usage::

        from floe_agentkit_actions.integrations.langchain import get_floe_langchain_tools

        tools = get_floe_langchain_tools(wallet_provider)
        agent = create_react_agent(llm, tools)
    """
    try:
        from coinbase_agentkit import AgentKit, AgentKitConfig
        from coinbase_agentkit_langchain import get_langchain_tools
    except ImportError as e:
        raise ImportError(
            "LangChain integration requires extra dependencies. "
            "Install with: pip install floe-agentkit-actions[langchain]"
        ) from e

    from .. import floe_action_provider

    agentkit = AgentKit(
        AgentKitConfig(
            wallet_provider=wallet_provider,
            action_providers=[floe_action_provider(config)],
        )
    )
    return get_langchain_tools(agentkit)
