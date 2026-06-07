"""CrewAI integration — budget-enabled Floe agents.

The pitch: one Floe credit line caps **everything a crew spends** — paid tool
calls (x402/USDC) AND LLM tokens (via the Floe-metered proxy) — with a hard,
server-side ceiling. The 3 AM infinite loop dies at $1, not $414.

Requires the optional extra::

    pip install floe-agentkit-actions[crewai]

Quickstart::

    from floe_agentkit_actions.integrations.crewai import (
        budget_enabled_agent, FloeBudget,
    )

    agent = budget_enabled_agent(
        role="Researcher",
        goal="Find the cheapest dataset",
        backstory="A frugal analyst.",
        budget=FloeBudget(usd_limit=5.0),
        wallet_provider=wallet_provider,
    )

Two cost planes, both under one ceiling:

* **Tool plane** — ``Floe402Tool`` + the converted AgentKit actions pay x402
  APIs from the credit line. The merchant allowlist (opt-in) restricts which
  vendors the agent may pay.
* **LLM plane** — ``FloeLLM`` routes GPT/Claude through the Floe-metered proxy
  at ``<floe-api>/v1/llm`` (debits the same credit line, refuses past the
  ceiling). The credit key is the ``Authorization: Bearer``; the upstream
  provider key is passed through via ``X-Floe-Provider-Key`` (Floe holds none).
  x402-native models need no proxy.

The hard cap (server-side spend limit + facilitator) is the real protection.
The advisory header / budget-aware backstory are *soft* signals the LLM honors
unreliably — useful for finishing on budget, not for enforcement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from ..floe_agent import DEFAULT_BASE_URL, FloeAgent
from ..types import FloeConfig
from ..x402_action_provider import X402ActionProvider, X402Config, x402_action_provider

USDC_DECIMALS = 6
USDC_SCALE = 10**USDC_DECIMALS


def _require_crewai() -> Any:
    """Import crewai lazily so it stays an optional extra."""
    try:
        import crewai  # noqa: F401
        from crewai import LLM, Agent  # noqa: F401
        from crewai.tools import BaseTool  # noqa: F401
    except ImportError as e:  # pragma: no cover - exercised only without the extra
        raise ImportError(
            "CrewAI integration requires extra dependencies. "
            "Install with: pip install floe-agentkit-actions[crewai]"
        ) from e
    return crewai


def _usd_to_raw(usd: float | str) -> str:
    """Convert a dollar amount to raw USDC integer units (6 decimals)."""
    scaled = (Decimal(str(usd)) * USDC_SCALE).to_integral_value()
    if scaled <= 0:
        raise ValueError(f"amount must be positive, got {usd!r}")
    return str(int(scaled))


def _looks_like_address(value: str) -> bool:
    return value.startswith("0x") and len(value) == 42


# ── AgentKit action → CrewAI tool ─────────────────────────────────────────────


def _empty_args_schema() -> Any:
    """A pydantic model with no fields, for actions that take no arguments."""
    from pydantic import BaseModel

    class _EmptyArgs(BaseModel):
        pass

    return _EmptyArgs


def _agentkit_tool_class() -> Any:
    """Build the generic ``BaseTool`` subclass that wraps an AgentKit action.

    Defined lazily (inside a function) because ``crewai.tools.BaseTool`` is an
    optional import. The action's bound ``invoke(args: dict) -> str`` is stored
    as a pydantic field; ``_run(**kwargs)`` forwards the kwargs as a dict.
    """
    from crewai.tools import BaseTool

    class _AgentKitTool(BaseTool):
        invoke_fn: Callable[[dict[str, Any]], str]

        def _run(self, **kwargs: Any) -> str:
            return self.invoke_fn(kwargs)

    return _AgentKitTool


def _action_to_tool(action: Any) -> Any:
    """Convert one AgentKit ``Action`` into a ``crewai.tools.BaseTool``.

    Sets ``name``/``description``/``args_schema`` from the action and routes
    ``_run`` to ``action.invoke(kwargs)``.
    """
    _require_crewai()
    tool_cls = _agentkit_tool_class()
    return tool_cls(
        name=action.name,
        description=action.description,
        args_schema=action.args_schema or _empty_args_schema(),
        invoke_fn=action.invoke,
    )


def get_floe_crewai_tools(
    wallet_provider: Any,
    config: FloeConfig | None = None,
    x402_config: X402Config | None = None,
) -> list[Any]:
    """Create CrewAI tools from Floe AgentKit actions.

    Mirrors ``get_floe_langchain_tools`` but converts each action directly to a
    ``crewai.tools.BaseTool`` (CrewAI has no Coinbase helper).
    """
    _require_crewai()
    from coinbase_agentkit import AgentKit, AgentKitConfig

    from .. import floe_action_provider

    agentkit = AgentKit(
        AgentKitConfig(
            wallet_provider=wallet_provider,
            action_providers=[
                floe_action_provider(config),
                x402_action_provider(x402_config),
            ],
        )
    )
    return [_action_to_tool(action) for action in agentkit.get_actions()]


# ── Floe402Tool — ergonomic per-call paid tool ────────────────────────────────


def _floe402_tool_class() -> Any:
    from crewai.tools import BaseTool
    from pydantic import BaseModel, Field

    class _Floe402Args(BaseModel):
        url: str | None = Field(default=None, description="Override the tool's configured URL.")
        body: str | None = Field(default=None, description="Optional request body override.")

    class Floe402Tool(BaseTool):
        """Pay-per-call x402 tool. The agent never holds USDC or an API key.

        The developer wires it with a ``floe_*`` credit key at construction;
        each ``_run`` triggers the x402 flow (auto-borrow + pay) through the
        facilitator and returns the response body. The agent just invokes it.
        """

        name: str = "floe_paid_fetch"
        description: str = (
            "Fetch a paid (x402) URL. Payment is handled automatically from the "
            "Floe credit line — you never hold USDC or an API key. Returns the "
            "response body and the amount paid."
        )
        args_schema: type[BaseModel] = _Floe402Args
        url: str
        method: str = "GET"
        headers: dict[str, str] | None = None
        body: str | None = None
        api_key: str
        base_url: str = DEFAULT_BASE_URL

        def _run(self, url: str | None = None, body: str | None = None) -> str:
            agent = FloeAgent(api_key=self.api_key, base_url=self.base_url)
            result = agent.fetch(
                url=url or self.url,
                method=self.method,
                headers=self.headers,
                body=body if body is not None else self.body,
            )
            note = f"*Paid via x402 — ${result.cost:.6f} USDC*\n\n" if result.cost else ""
            return f"{note}{result.body}"

    return Floe402Tool


def __getattr__(name: str) -> Any:
    # PEP 562: expose the crewai-typed classes (Floe402Tool, FloeLLM) lazily so
    # importing this module does not require crewai until the symbol is used.
    if name == "Floe402Tool":
        _require_crewai()
        return _floe402_tool_class()
    if name == "FloeLLM":
        _require_crewai()
        return _floe_llm_class()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# ── FloeLLM — route GPT/Claude through the Floe metered proxy ──────────────────


def _floe_llm_class() -> Any:
    from crewai import LLM

    class FloeLLM:
        """Construct a ``crewai.LLM`` routed through the Floe metered proxy.

        NOT a true subclass: ``crewai.LLM.__new__`` is itself a provider-routing
        factory that returns native-SDK instances, so subclassing is unreliable
        and breaks ``isinstance``. ``__new__`` returns a configured
        ``crewai.LLM`` instead — it quacks like the constructor.

        Three credentials, three slots (D3 metered-proxy contract):

        * ``proxy_base_url`` — the D3 endpoint, ``<floe-api>/v1/llm`` (the proxy
          exposes the OpenAI-compatible ``POST /v1/llm/chat/completions``). Pass
          it in; never hardcoded. For x402-native models you can point it
          straight at the model endpoint.
        * ``credit_key`` — the ``floe_*`` credit/agent key. Sent as
          ``Authorization: Bearer`` (crewai.LLM ``api_key``); the proxy
          authenticates and debits the credit line with it.
        * ``provider_key`` — the upstream OpenAI/Anthropic key. Sent
          pass-through in the ``X-Floe-Provider-Key`` request header so the proxy
          can reach upstream. **Floe holds none of these keys.**
        """

        def __new__(
            cls,
            model: str,
            proxy_base_url: str | None = None,
            credit_key: str | None = None,
            provider_key: str | None = None,
            base_url: str | None = None,
            api_key: str | None = None,
            extra_headers: dict[str, str] | None = None,
            **kwargs: Any,
        ) -> Any:
            # Pass the upstream provider key through to the proxy via
            # X-Floe-Provider-Key (LiteLLM forwards extra_headers). Merge so a
            # caller-supplied header set is preserved.
            headers = dict(extra_headers) if extra_headers else {}
            if provider_key is not None:
                headers["X-Floe-Provider-Key"] = provider_key
            if headers:
                kwargs["extra_headers"] = headers
            # crewai.LLM's pydantic __init__ signature confuses mypy (it's
            # actually a __new__ factory taking (model, **kwargs)).
            return LLM(  # type: ignore[misc,arg-type]
                model,
                base_url=base_url or proxy_base_url,
                api_key=api_key or credit_key,
                **kwargs,
            )

    return FloeLLM


# ── FloeBudget — provision one ceiling across the whole crew ───────────────────


@dataclass
class FloeBudget:
    """One spend ceiling for a crew: credit line + session cap + (opt-in) allowlist.

    Fields:
        usd_limit: the hard dollar ceiling. Used both as the on-chain credit
            line and the session spend cap, so tool + LLM spend share one wall.
        allow: optional ``{host_or_payee: cap}`` map. ``None`` (the default)
            allows any vendor — no enumeration, low onboarding friction. When
            provided, becomes default-deny on both host and payee.
        max_rate_bps: max borrow rate for the credit delegation.
        expiry_seconds: delegation lifetime.
        name: agent label for the credit delegation (defaults to the provider's).
    """

    usd_limit: float
    allow: dict[str, str] | None = None
    max_rate_bps: int = 1500
    expiry_seconds: int = 90 * 86400
    name: str | None = None
    _provisioned: bool = field(default=False, repr=False)

    def provision(self, provider: X402ActionProvider, wallet_provider: Any) -> None:
        """Issue the credit delegation, session cap, and (opt-in) allowlist.

        Idempotent: a second call is a no-op. Allowlist entries are only written
        when ``allow`` is provided, and the mode is flipped to 'both' so both the
        host (pre-fetch) and payee (pre-sign) gates enforce them.
        """
        if self._provisioned:
            return

        name = self.name or getattr(provider, "_agent_name", "") or "crewai-agent"
        facilitator_url = getattr(provider, "_facilitator_url", "")
        expiry_days = max(1, self.expiry_seconds // 86400)

        provider.grant_credit_delegation(
            wallet_provider,
            {
                "name": name,
                "facilitator_url": facilitator_url,
                "borrow_limit": str(self.usd_limit),
                "max_rate_bps": str(self.max_rate_bps),
                "expiry_days": str(expiry_days),
            },
        )
        provider.set_spend_limit(
            wallet_provider,
            {"limit_raw": _usd_to_raw(self.usd_limit)},
        )

        if self.allow is not None:
            for match_key, cap in self.allow.items():
                provider.add_allowlist_entry(
                    wallet_provider,
                    {
                        "kind": "vendor" if _looks_like_address(match_key) else "api",
                        "match_key": match_key,
                        "limit_raw": _usd_to_raw(cap),
                    },
                )
            provider.set_allowlist_mode(wallet_provider, {"mode": "both"})

        self._provisioned = True


# ── floe_budget_status tool ───────────────────────────────────────────────────


@dataclass
class _BudgetState:
    """Per-agent spend ledger + last advisory, shared with the status tool."""

    usd_limit: float | None = None
    last_advisory: str | None = None
    ledger: list[dict[str, Any]] = field(default_factory=list)


def _make_budget_status_tool(state: _BudgetState) -> Any:
    """Build a ``floe_budget_status`` tool reporting tightest-cap proximity."""
    from crewai.tools import BaseTool
    from pydantic import BaseModel

    class _StatusArgs(BaseModel):
        pass

    class _BudgetStatusTool(BaseTool):
        name: str = "floe_budget_status"
        description: str = (
            "Check how close you are to your spend limit. Returns the tightest-cap "
            "proximity (from the last paid-call budget advisory) and the spend so "
            "far this run. Use it BEFORE expensive calls; stop when near the limit."
        )
        args_schema: type[BaseModel] = _StatusArgs

        def _run(self) -> str:
            lines = ["## Budget Status\n"]
            if state.usd_limit is not None:
                lines.append(f"**Limit**: ${state.usd_limit:.2f}")
            spent = sum(float(e.get("cost", 0.0)) for e in state.ledger)
            lines.append(f"**Spent this run**: ${spent:.6f}")
            if state.usd_limit:
                remaining = max(0.0, state.usd_limit - spent)
                lines.append(f"**Remaining (approx)**: ${remaining:.6f}")
            if state.last_advisory:
                lines.append(f"**Last advisory**: {state.last_advisory}")
            else:
                lines.append("_No paid-call advisory observed yet this run._")
            return "\n".join(lines)

    return _BudgetStatusTool()


# ── budget_enabled_agent factory ──────────────────────────────────────────────

_BUDGET_AWARE_BACKSTORY = (
    "\n\nYou operate under a hard spend budget. Before any paid tool call or "
    "expensive step, check your remaining budget with floe_budget_status. Prefer "
    "cheaper paths, avoid redundant calls, and stop as soon as you are near the "
    "limit — the budget is enforced server-side and calls will be refused past it."
)


def budget_enabled_agent(
    role: str,
    goal: str,
    backstory: str,
    budget: FloeBudget,
    wallet_provider: Any,
    llm: Any | None = None,
    budget_aware: bool = True,
    config: FloeConfig | None = None,
    x402_config: X402Config | None = None,
    llm_model: str = "openai/gpt-4o",
    proxy_base_url: str | None = None,
    provider_key: str | None = None,
    **agent_kwargs: Any,
) -> Any:
    """Provision a budget and return a plain ``crewai.Agent`` wired to Floe.

    Returns a PLAIN ``crewai.Agent`` (no subclass) — enforcement lives in the
    credit line + facilitator, never in agent internals.

    The budget is provisioned (credit delegation + session cap + opt-in
    allowlist). Tools are the converted Floe AgentKit actions plus, when
    ``budget_aware``, a ``floe_budget_status`` tool and a budget-aware backstory
    addition. ``llm`` defaults to a ``FloeLLM`` routed through ``proxy_base_url``
    when one is supplied; otherwise CrewAI's default LLM is used.

    Args:
        proxy_base_url: D3 Floe-metered proxy base URL (``<floe-api>/v1/llm``).
            Pass it in — it is not hardcoded because the proxy ships separately.
            Without it (and without an explicit ``llm``), no FloeLLM is built.
        provider_key: upstream OpenAI/Anthropic key, passed through to the proxy
            via ``X-Floe-Provider-Key`` so it can reach upstream. Floe holds none.
    """
    _require_crewai()
    from crewai import Agent

    provider = x402_action_provider(x402_config)
    budget.provision(provider, wallet_provider)

    state = _BudgetState(usd_limit=budget.usd_limit)
    tools = get_floe_crewai_tools(wallet_provider, config, x402_config)
    if budget_aware:
        tools.append(_make_budget_status_tool(state))
        backstory = backstory + _BUDGET_AWARE_BACKSTORY

    if llm is None and proxy_base_url is not None:
        credit_key = getattr(x402_config, "facilitator_api_key", "") if x402_config else ""
        floe_llm = __getattr__("FloeLLM")
        llm = floe_llm(
            llm_model,
            proxy_base_url=proxy_base_url,
            credit_key=credit_key,
            provider_key=provider_key,
        )

    def _step_callback(step: Any) -> None:
        # Per-agent spend ledger. We record the raw step so a downstream
        # consumer can price it; cost defaults to 0 until the proxy/x402 path
        # reports one via the advisory.
        state.ledger.append({"step": step, "cost": 0.0})

    if llm is not None:
        agent_kwargs.setdefault("llm", llm)
    agent_kwargs.setdefault("step_callback", _step_callback)

    return Agent(role=role, goal=goal, backstory=backstory, tools=tools, **agent_kwargs)


# Floe402Tool and FloeLLM are provided via module __getattr__ (lazy, so crewai
# stays optional), hence the F822 suppressions — they are real public names.
__all__ = [
    "get_floe_crewai_tools",
    "Floe402Tool",  # noqa: F822
    "FloeLLM",  # noqa: F822
    "FloeBudget",
    "budget_enabled_agent",
    "_action_to_tool",
]
