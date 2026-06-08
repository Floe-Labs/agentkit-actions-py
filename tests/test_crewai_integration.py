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
        proxy_base_url="https://proxy.floe.test/v1/llm",
        credit_key="floe_credit",
    )
    # crewai.LLM.__new__ returns provider-specific instances (e.g.
    # OpenAICompletion), so it's a BaseLLM, not a crewai.LLM instance.
    assert isinstance(llm, crewai.BaseLLM)
    assert llm.base_url == "https://proxy.floe.test/v1/llm"
    assert llm.api_key == "floe_credit"


def test_floe_llm_passes_provider_key_header() -> None:
    from floe_agentkit_actions.integrations.crewai import FloeLLM

    llm = FloeLLM(
        "openai/gpt-4o",
        proxy_base_url="https://proxy.floe.test/v1/llm",
        credit_key="floe_credit",
        provider_key="sk-x",
    )
    # extra_headers lands in additional_params (LiteLLM forwards it upstream).
    assert llm.additional_params["extra_headers"] == {"X-Floe-Provider-Key": "sk-x"}


def test_floe_llm_merges_caller_extra_headers() -> None:
    from floe_agentkit_actions.integrations.crewai import FloeLLM

    llm = FloeLLM(
        "openai/gpt-4o",
        proxy_base_url="https://proxy.floe.test/v1/llm",
        credit_key="floe_credit",
        provider_key="sk-x",
        extra_headers={"X-Trace": "abc"},
    )
    assert llm.additional_params["extra_headers"] == {
        "X-Trace": "abc",
        "X-Floe-Provider-Key": "sk-x",
    }


# ── FloeBudget.provision ───────────────────────────────────────────────────────


def _budget_provider(key: str = "floe_managed_alpha", agent_id: int = 42) -> Any:
    """A fake X402 provider whose grant_credit_delegation captures a managed key.

    Mirrors the real provider: grant stashes the freshly-minted managed-agent
    key on ``_facilitator_api_key`` and returns the markdown success string.
    """
    provider = MagicMock()
    provider._agent_name = "alpha"
    provider._facilitator_url = "https://credit-api.floelabs.xyz"
    provider._facilitator_api_key = ""

    def _grant(_wallet: Any, _args: dict) -> str:
        provider._facilitator_api_key = key
        return f"## Floe Agent Registered\n\n**Agent ID**: {agent_id}\n**Name**: alpha\n"

    provider.grant_credit_delegation.side_effect = _grant
    return provider


def test_budget_provision_creates_managed_agent_and_caps_it() -> None:
    from floe_agentkit_actions.integrations.crewai import FloeBudget

    provider = _budget_provider(key="floe_managed_alpha", agent_id=42)
    wallet = MagicMock()
    budget = FloeBudget(usd_limit=5.0, max_rate_bps=1200, expiry_seconds=30 * 86400)

    budget.provision(provider, wallet)

    provider.grant_credit_delegation.assert_called_once()
    _, grant_args = provider.grant_credit_delegation.call_args[0]
    assert grant_args["name"] == "alpha"
    assert grant_args["borrow_limit"] == "5.0"
    assert grant_args["max_rate_bps"] == "1200"
    assert grant_args["expiry_days"] == "30"

    # The managed agent's identity is captured for the runtime to act as.
    assert budget.agent_key == "floe_managed_alpha"
    assert budget.agent_id == 42
    assert budget.facilitator_url == "https://credit-api.floelabs.xyz"

    provider.set_spend_limit.assert_called_once()
    _, spend_args = provider.set_spend_limit.call_args[0]
    assert spend_args["limit_raw"] == "5000000"  # $5 in raw USDC

    # No allowlist when allow is None (default = allow any vendor).
    provider.add_allowlist_entry.assert_not_called()
    provider.set_allowlist_mode.assert_not_called()


def test_budget_provision_raises_when_grant_fails() -> None:
    from floe_agentkit_actions.integrations.crewai import FloeBudget, FloeProvisionError

    provider = MagicMock()
    provider._agent_name = "alpha"
    provider._facilitator_url = "https://credit-api.floelabs.xyz"
    provider._facilitator_api_key = ""
    provider.grant_credit_delegation.return_value = "Agent creation failed: quota exceeded"

    budget = FloeBudget(usd_limit=5.0)
    with pytest.raises(FloeProvisionError):
        budget.provision(provider, MagicMock())
    # Must not cap a non-existent agent.
    provider.set_spend_limit.assert_not_called()


def test_two_budgets_under_one_wallet_do_not_collide() -> None:
    """Per-role isolation: each budget gets its OWN managed-agent key."""
    from floe_agentkit_actions.integrations.crewai import FloeBudget

    wallet = MagicMock()  # ONE developer wallet shared by both roles

    researcher = FloeBudget(usd_limit=1.0, name="researcher")
    buyer = FloeBudget(usd_limit=5.0, name="buyer")

    researcher.provision(_budget_provider(key="floe_researcher", agent_id=1), wallet)
    buyer.provision(_budget_provider(key="floe_buyer", agent_id=2), wallet)

    assert researcher.agent_key == "floe_researcher"
    assert buyer.agent_key == "floe_buyer"
    assert researcher.agent_key != buyer.agent_key
    assert researcher.agent_id != buyer.agent_id


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


def test_zero_budget_is_hard_zero_spend() -> None:
    """$0 = approval-only role: no credit line, no managed agent, no key."""
    from floe_agentkit_actions.integrations.crewai import FloeBudget, _usd_to_raw

    provider = _budget_provider()
    budget = FloeBudget(usd_limit=0, name="manager")
    budget.provision(provider, MagicMock())

    provider.grant_credit_delegation.assert_not_called()
    provider.set_spend_limit.assert_not_called()
    assert budget.agent_key is None  # fail-closed: no spend capability

    # _usd_to_raw allows zero, rejects negatives.
    assert _usd_to_raw(0) == "0"
    with pytest.raises(ValueError):
        _usd_to_raw(-1)


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


def test_budget_enabled_agent_threads_managed_key_into_tools(
    mock_wallet: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The role's tools act AS its managed agent, and two roles stay isolated."""
    import floe_agentkit_actions.integrations.crewai as crewai_mod
    from floe_agentkit_actions.integrations.crewai import FloeBudget, budget_enabled_agent

    # x402_action_provider returns a distinct fake managed provider per role.
    providers = [
        _budget_provider(key="floe_researcher", agent_id=1),
        _budget_provider(key="floe_buyer", agent_id=2),
    ]
    monkeypatch.setattr(crewai_mod, "x402_action_provider", lambda cfg=None: providers.pop(0))

    # Capture the x402_config the tools are built with for each role.
    seen_configs: list[Any] = []

    def _fake_tools(wallet: Any, config: Any = None, x402_config: Any = None) -> list[Any]:
        seen_configs.append(x402_config)
        return []

    monkeypatch.setattr(crewai_mod, "get_floe_crewai_tools", _fake_tools)

    wallet = MagicMock()
    budget_enabled_agent(
        role="Researcher", goal="g", backstory="b",
        budget=FloeBudget(usd_limit=1.0, name="researcher"),
        wallet_provider=wallet, x402_config=X402Config(facilitator_url="https://credit-api.floelabs.xyz"),
    )
    budget_enabled_agent(
        role="Buyer", goal="g", backstory="b",
        budget=FloeBudget(usd_limit=5.0, name="buyer"),
        wallet_provider=wallet, x402_config=X402Config(facilitator_url="https://credit-api.floelabs.xyz"),
    )

    assert seen_configs[0].facilitator_api_key == "floe_researcher"
    assert seen_configs[1].facilitator_api_key == "floe_buyer"
    assert seen_configs[0].facilitator_api_key != seen_configs[1].facilitator_api_key


def test_floe402_tool_records_structured_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    import floe_agentkit_actions.integrations.crewai as crewai_mod
    from floe_agentkit_actions.integrations.crewai import Floe402Tool

    class _FakeResult:
        body = "data"
        cost = 0.0025
        cost_raw = "2500"

    class _FakeAgent:
        def __init__(self, api_key: str, base_url: str) -> None:
            pass

        def fetch(self, **kwargs: Any) -> Any:
            return _FakeResult()

    monkeypatch.setattr(crewai_mod, "FloeAgent", _FakeAgent)

    ledger: list[dict[str, Any]] = []
    tool = Floe402Tool(url="https://api.example.com", api_key="floe_k", ledger=ledger)
    tool.run()

    assert ledger == [
        {"url": "https://api.example.com", "cost": 0.0025, "cost_raw": "2500", "tool": "floe_paid_fetch"}
    ]


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


def test_add_allowlist_entry_posts_valid_vendor(x402_provider: X402ActionProvider) -> None:
    payee = "0x" + "ab" * 20
    x402_provider._facilitator_fetch = MagicMock(  # type: ignore[method-assign]
        return_value={
            "status": 201,
            "body": {"policy": {"id": 9, "kind": "vendor", "matchKey": payee, "limitRaw": "1000000"}},
            "headers": {},
        }
    )
    out = x402_provider.add_allowlist_entry(
        MagicMock(),
        {"kind": "vendor", "match_key": payee, "limit_raw": "1000000", "match_kind": "recipient"},
    )

    x402_provider._facilitator_fetch.assert_called_once_with(
        "/agents/policies",
        method="POST",
        body={"kind": "vendor", "matchKey": payee, "limitRaw": "1000000", "matchKind": "recipient"},
    )
    assert "#9" in out


def test_add_allowlist_entry_rejects_vendor_non_address(x402_provider: X402ActionProvider) -> None:
    x402_provider._facilitator_fetch = MagicMock()  # type: ignore[method-assign]
    out = x402_provider.add_allowlist_entry(
        MagicMock(), {"kind": "vendor", "match_key": "not-an-address", "limit_raw": "1000000"}
    )

    assert out.startswith("Error:")
    assert "wallet address" in out
    x402_provider._facilitator_fetch.assert_not_called()  # local validation, no HTTP


def test_add_allowlist_entry_rejects_vendor_wrong_match_kind(x402_provider: X402ActionProvider) -> None:
    payee = "0x" + "cd" * 20
    x402_provider._facilitator_fetch = MagicMock()  # type: ignore[method-assign]
    out = x402_provider.add_allowlist_entry(
        MagicMock(),
        {"kind": "vendor", "match_key": payee, "limit_raw": "1000000", "match_kind": "host_suffix"},
    )

    assert out.startswith("Error:")
    x402_provider._facilitator_fetch.assert_not_called()


def test_add_allowlist_entry_rejects_api_recipient_match_kind(x402_provider: X402ActionProvider) -> None:
    x402_provider._facilitator_fetch = MagicMock()  # type: ignore[method-assign]
    out = x402_provider.add_allowlist_entry(
        MagicMock(),
        {"kind": "api", "match_key": "x.com", "limit_raw": "2000000", "match_kind": "recipient"},
    )

    assert out.startswith("Error:")
    x402_provider._facilitator_fetch.assert_not_called()


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
