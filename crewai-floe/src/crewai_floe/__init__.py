"""crewai-floe — budget-enabled CrewAI agents backed by a Floe credit line.

This package is a thin re-export of the CrewAI integration in
``floe-agentkit-actions`` (``floe_agentkit_actions.integrations.crewai``). It
exists purely as an ergonomic, discoverable install for CrewAI users:

    pip install crewai-floe

    from crewai_floe import budget_enabled_agent, FloeBudget

There is NO logic here — the single source of truth is the integration module.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "budget_enabled_agent",
    "FloeBudget",
    "Floe402Tool",
    "FloeLLM",
    "get_floe_crewai_tools",
]


def __getattr__(name: str) -> Any:
    # Delegate every public name to the upstream integration module. Some of
    # those names (Floe402Tool, FloeLLM) are themselves provided lazily, so we
    # forward attribute access rather than importing eagerly.
    if name in __all__:
        from floe_agentkit_actions.integrations import crewai as _crewai

        return getattr(_crewai, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
