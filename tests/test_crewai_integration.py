"""Tests for the CrewAI integration (D4) and D1 merchant-allowlist actions.

No chain / no live network: AgentKit action conversion is offline, the
facilitator HTTP layer and the FloeAgent runtime client are mocked.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel, Field

from floe_agentkit_actions.x402_action_provider import X402ActionProvider, X402Config

crewai = pytest.importorskip("crewai")
# AgentKitConfig validates wallet_provider with isinstance(WalletProvider). The
# test MockWalletProvider isn't a real subclass, so register it as a virtual
# subclass (WalletProvider is an ABC) — keeps these tests fully offline.
from coinbase_agentkit import WalletProvider  # noqa: E402
from crewai.tools import BaseTool  # noqa: E402

from tests.conftest import MockWalletProvider  # noqa: E402

WalletProvider.register(MockWalletProvider)


# ── _action_to_tool ───────────────────────────────────────────────────────────


class _DemoArgs(BaseModel):
    x: str = Field(description="an input")


def _fake_action(name: str, schema: type[BaseModel] | None, invoke: Any) -> Any:
    action = MagicMock()
    action.name = name
    action.description = f"description for {name}"
    action.args_schema = schema
    action.invoke = invoke
    return action


def test_action_to_tool_yields_valid_basetool() -> None:
    from floe_agentkit_actions.integrations.crewai import _action_to_tool

    action = _fake_action("demo", _DemoArgs, lambda args: f"got {args}")
    tool = _action_to_tool(action)

    assert isinstance(tool, BaseTool)
    assert tool.name == "demo"
    # CrewAI augments description with name + arg schema; ours must be in it.
    assert "description for demo" in tool.description
    assert tool.args_schema is _DemoArgs
    assert tool.run(x="hi") == "got {'x': 'hi'}"


def test_action_to_tool_handles_no_schema() -> None:
    from floe_agentkit_actions.integrations.crewai import _action_to_tool

    action = _fake_action("empty", None, lambda args: "ok")
    tool = _action_to_tool(action)

    assert isinstance(tool, BaseTool)
    assert tool.run() == "ok"


def test_get_floe_crewai_tools_converts_all_actions(mock_wallet: Any) -> None:
    from floe_agentkit_actions.integrations.crewai import get_floe_crewai_tools

    tools = get_floe_crewai_tools(mock_wallet)
    assert len(tools) > 0
    assert all(isinstance(t, BaseTool) for t in tools)
    names = {t.name for t in tools}
    # AgentKit prefixes action names with the provider class name; both
    # providers must be represented.
    assert any(n.endswith("x402_fetch") for n in names)
    assert any(n.endswith("set_allowlist_mode") for n in names)


# ── Floe402Tool ───────────────────────────────────────────────────────────────


def test_floe402_tool_runs_x402_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    import floe_agentkit_actions.integrations.crewai as crewai_mod
    from floe_agentkit_actions.integrations.crewai import Floe402Tool

    fetch_calls: list[dict[str, Any]] = []

    class _FakeResult:
        body = '{"data": 42}'
        cost = 0.001234

    class _FakeAgent:
        def __init__(self, api_key: str, base_url: str) -> None:
            assert api_key == "floe_testkey"

        def fetch(self, **kwargs: Any) -> Any:
            fetch_calls.append(kwargs)
            return _FakeResult()

    monkeypatch.setattr(crewai_mod, "FloeAgent", _FakeAgent)

    tool = Floe402Tool(url="https://api.example.com/data", api_key="floe_testkey")
    out = tool.run()

    assert len(fetch_calls) == 1
    assert fetch_calls[0]["url"] == "https://api.example.com/data"
    assert "0.001234" in out
    assert '{"data": 42}' in out


def test_floe402_tool_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    import floe_agentkit_actions.integrations.crewai as crewai_mod
    from floe_agentkit_actions.integrations.crewai import Floe402Tool

    seen: list[str] = []

    class _FakeResult:
        body = "ok"
        cost = 0.0

    class _FakeAgent:
        def __init__(self, api_key: str, base_url: str) -> None:
            pass

        def fetch(self, **kwargs: Any) -> Any:
            seen.append(kwargs["url"])
            return _FakeResult()

    monkeypatch.setattr(crewai_mod, "FloeAgent", _FakeAgent)

    tool = Floe402Tool(url="https://default.example", api_key="floe_k")
    tool.run(url="https://override.example")
    assert seen == ["https://override.example"]


# ── FloeLLM ────────────────────────────────────────────────────────────────────


def test_floe_llm_routes_through_proxy() -> None:
    from floe_agentkit_actions.integrations.crewai import FloeLLM

    llm = FloeLLM(
        "openai/gpt-4o",
        proxy_base_url="https://proxy.floe.test/v1",
        credit_key="floe_credit",
    )
    # crewai.LLM.__new__ returns provider-specific instances (e.g.
    # OpenAICompletion), so it's a BaseLLM, not a crewai.LLM instance.
    assert isinstance(llm, crewai.BaseLLM)
    assert llm.base_url == "https://proxy.floe.test/v1"
    assert llm.api_key == "floe_credit"


# ── FloeBudget.provision ───────────────────────────────────────────────────────


def _budget_provider() -> Any:
    provider = MagicMock()
    provider._agent_name = "alpha"
    provider._facilitator_url = "https://credit-api.floelabs.xyz"
    return provider


def test_budget_provision_issues_delegation_and_spend_limit() -> None:
    from floe_agentkit_actions.integrations.crewai import FloeBudget

    provider = _budget_provider()
    wallet = MagicMock()
    budget = FloeBudget(usd_limit=5.0, max_rate_bps=1200, expiry_seconds=30 * 86400)

    budget.provision(provider, wallet)

    provider.grant_credit_delegation.assert_called_once()
    _, grant_args = provider.grant_credit_delegation.call_args[0]
    assert grant_args["name"] == "alpha"
    assert grant_args["borrow_limit"] == "5.0"
    assert grant_args["max_rate_bps"] == "1200"
    assert grant_args["expiry_days"] == "30"

    provider.set_spend_limit.assert_called_once()
    _, spend_args = provider.set_spend_limit.call_args[0]
    assert spend_args["limit_raw"] == "5000000"  # $5 in raw USDC

    # No allowlist when allow is None (default = allow any vendor).
    provider.add_allowlist_entry.assert_not_called()
    provider.set_allowlist_mode.assert_not_called()


def test_budget_provision_writes_allowlist_when_provided() -> None:
    from floe_agentkit_actions.integrations.crewai import FloeBudget

    provider = _budget_provider()
    wallet = MagicMock()
    payee = "0x" + "ab" * 20
    budget = FloeBudget(
        usd_limit=10.0,
        allow={"api.example.com": "2", payee: "1"},
    )

    budget.provision(provider, wallet)

    assert provider.add_allowlist_entry.call_count == 2
    kinds = {c[0][1]["kind"]: c[0][1] for c in provider.add_allowlist_entry.call_args_list}
    assert kinds["api"]["match_key"] == "api.example.com"
    assert kinds["api"]["limit_raw"] == "2000000"
    assert kinds["vendor"]["match_key"] == payee
    assert kinds["vendor"]["limit_raw"] == "1000000"

    provider.set_allowlist_mode.assert_called_once()
    _, mode_args = provider.set_allowlist_mode.call_args[0]
    assert mode_args["mode"] == "both"


def test_budget_provision_is_idempotent() -> None:
    from floe_agentkit_actions.integrations.crewai import FloeBudget

    provider = _budget_provider()
    wallet = MagicMock()
    budget = FloeBudget(usd_limit=1.0)

    budget.provision(provider, wallet)
    budget.provision(provider, wallet)

    provider.grant_credit_delegation.assert_called_once()
    provider.set_spend_limit.assert_called_once()


# ── budget_enabled_agent ────────────────────────────────────────────────────────


def test_budget_enabled_agent_returns_plain_agent(mock_wallet: Any) -> None:
    from floe_agentkit_actions.integrations.crewai import FloeBudget, budget_enabled_agent

    budget = FloeBudget(usd_limit=5.0)
    budget._provisioned = True  # skip the network-bound provisioning

    agent = budget_enabled_agent(
        role="Researcher",
        goal="Find cheap data",
        backstory="A frugal analyst.",
        budget=budget,
        wallet_provider=mock_wallet,
    )

    assert isinstance(agent, crewai.Agent)
    assert type(agent) is crewai.Agent  # plain Agent, not a subclass
    tool_names = {t.name for t in agent.tools}
    assert "floe_budget_status" in tool_names
    assert any(n.endswith("x402_fetch") for n in tool_names)
    assert "budget" in agent.backstory.lower()


def test_budget_enabled_agent_can_disable_budget_awareness(mock_wallet: Any) -> None:
    from floe_agentkit_actions.integrations.crewai import FloeBudget, budget_enabled_agent

    budget = FloeBudget(usd_limit=5.0)
    budget._provisioned = True

    agent = budget_enabled_agent(
        role="R",
        goal="g",
        backstory="plain.",
        budget=budget,
        wallet_provider=mock_wallet,
        budget_aware=False,
    )

    tool_names = {t.name for t in agent.tools}
    assert "floe_budget_status" not in tool_names
    assert agent.backstory == "plain."


# ── D1 allowlist actions (HTTP mocked) ─────────────────────────────────────────


@pytest.fixture
def x402_provider() -> X402ActionProvider:
    return X402ActionProvider(
        X402Config(
            facilitator_url="https://credit-api.floelabs.xyz",
            facilitator_api_key="floe_key",
        )
    )


def test_set_allowlist_mode_calls_endpoint(x402_provider: X402ActionProvider) -> None:
    x402_provider._facilitator_fetch = MagicMock(  # type: ignore[method-assign]
        return_value={"status": 200, "body": {"mode": "both"}, "headers": {}}
    )
    out = x402_provider.set_allowlist_mode(MagicMock(), {"mode": "both"})

    x402_provider._facilitator_fetch.assert_called_once_with(
        "/agents/allowlist-mode", method="PUT", body={"mode": "both"}
    )
    assert "both" in out


def test_get_allowlist_mode_calls_endpoint(x402_provider: X402ActionProvider) -> None:
    x402_provider._facilitator_fetch = MagicMock(  # type: ignore[method-assign]
        return_value={"status": 200, "body": {"mode": "host"}, "headers": {}}
    )
    out = x402_provider.get_allowlist_mode(MagicMock(), {})

    x402_provider._facilitator_fetch.assert_called_once_with("/agents/allowlist-mode")
    assert "host" in out


def test_add_allowlist_entry_posts_policy(x402_provider: X402ActionProvider) -> None:
    x402_provider._facilitator_fetch = MagicMock(  # type: ignore[method-assign]
        return_value={
            "status": 201,
            "body": {"policy": {"id": 7, "kind": "api", "matchKey": "x.com", "limitRaw": "2000000"}},
            "headers": {},
        }
    )
    out = x402_provider.add_allowlist_entry(
        MagicMock(), {"kind": "api", "match_key": "x.com", "limit_raw": "2000000"}
    )

    x402_provider._facilitator_fetch.assert_called_once_with(
        "/agents/policies",
        method="POST",
        body={"kind": "api", "matchKey": "x.com", "limitRaw": "2000000"},
    )
    assert "#7" in out


def test_remove_allowlist_entry_deletes_policy(x402_provider: X402ActionProvider) -> None:
    x402_provider._facilitator_fetch = MagicMock(  # type: ignore[method-assign]
        return_value={"status": 200, "body": {"status": "revoked"}, "headers": {}}
    )
    out = x402_provider.remove_allowlist_entry(MagicMock(), {"policy_id": 7})

    x402_provider._facilitator_fetch.assert_called_once_with(
        "/agents/policies/7", method="DELETE"
    )
    assert "#7" in out


def test_list_allowlist_filters_to_allowlist_kinds(x402_provider: X402ActionProvider) -> None:
    x402_provider._facilitator_fetch = MagicMock(  # type: ignore[method-assign]
        return_value={
            "status": 200,
            "body": {
                "policies": [
                    {"id": 1, "kind": "api", "matchKey": "x.com", "matchKind": "host_suffix", "limitRaw": "2000000"},
                    {"id": 2, "kind": "session", "matchKey": None, "limitRaw": "5000000"},
                    {"id": 3, "kind": "vendor", "matchKey": "0xabc", "matchKind": "recipient", "limitRaw": "1000000"},
                ]
            },
            "headers": {},
        }
    )
    out = x402_provider.list_allowlist(MagicMock(), {})

    x402_provider._facilitator_fetch.assert_called_once_with("/agents/policies")
    assert "#1" in out
    assert "#3" in out
    assert "#2" not in out  # session policy excluded


# ── lazy top-level export ──────────────────────────────────────────────────────


def test_lazy_top_level_exports() -> None:
    from floe_agentkit_actions import (  # noqa: F401
        Floe402Tool,
        FloeBudget,
        FloeLLM,
        budget_enabled_agent,
        get_floe_crewai_tools,
    )
