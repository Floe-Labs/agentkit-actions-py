"""OpenAI Agents SDK integration helper."""

from __future__ import annotations

from typing import Any

from ..types import FloeConfig


def get_floe_openai_tools(
    wallet_provider: Any,
    config: FloeConfig | None = None,
) -> list[dict[str, Any]]:
    """Create OpenAI function-calling tool definitions from Floe AgentKit actions.

    Usage::

        from floe_agentkit_actions.integrations.openai_agents import get_floe_openai_tools

        tools = get_floe_openai_tools(wallet_provider)
        # Use with openai.chat.completions.create(tools=tools, ...)
    """
    from .. import floe_action_provider

    provider = floe_action_provider(config)
    tools: list[dict[str, Any]] = []

    for action in provider.get_actions():
        schema = action.schema
        json_schema = schema.model_json_schema() if hasattr(schema, "model_json_schema") else {}

        tools.append(
            {
                "type": "function",
                "function": {
                    "name": action.name,
                    "description": action.description,
                    "parameters": json_schema,
                },
            }
        )

    return tools
