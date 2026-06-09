# crewai-floe

**One Floe credit line caps everything your crew spends — LLM tokens *and* paid tool calls — with a hard, server-side ceiling. The 3 AM infinite loop dies at $1, not $414.**

CrewAI's #1 community complaint is runaway cost from agentic loops (a real "$414 on Gemini" overnight). `crewai-floe` puts a single dollar wall around an entire crew: when the credit line is exhausted, the next paid call and the next LLM call are *refused* — the loop halts instead of draining your card.

```bash
pip install crewai-floe
```

## Quickstart

```python
from crewai import Crew, Task
from crewai_floe import budget_enabled_agent, FloeBudget

# wallet_provider: your Coinbase AgentKit EvmWalletProvider
agent = budget_enabled_agent(
    role="Researcher",
    goal="Find the cheapest dataset that answers the question",
    backstory="A frugal analyst who stops when the budget runs low.",
    budget=FloeBudget(usd_limit=5.0),   # hard ceiling for the whole agent
    wallet_provider=wallet_provider,
)

crew = Crew(agents=[agent], tasks=[Task(description="...", agent=agent)])
crew.kickoff()
```

`budget_enabled_agent` returns a **plain `crewai.Agent`** — no subclass, nothing to break on a CrewAI version bump. Enforcement lives in the Floe credit line and facilitator, never in agent internals.

## Two cost planes, one ceiling

| Plane | How it's capped |
|---|---|
| **Tool plane** (x402 APIs, 13k+ vendors) | `Floe402Tool` + converted Floe AgentKit actions pay from the credit line. The agent never holds USDC or an API key. |
| **LLM plane** (GPT / Claude) | `FloeLLM` routes through the Floe-metered proxy that debits the same credit line and refuses past the ceiling. x402-native models (Venice, etc.) need no proxy. |

## Merchant allowlist (opt-in)

By default the agent may pay **any** vendor — zero onboarding friction. Supply an allowlist and it becomes **default-deny on both host (pre-fetch) and payee (pre-sign)**:

```python
FloeBudget(
    usd_limit=10.0,
    allow={
        "api.openweathermap.org": "2",     # host cap $2
        "0xVendorWallet...": "1",          # payee cap $1
    },
)
```

An off-allowlist host is blocked before the first fetch; a payment redirected to an unlisted payee is blocked before signing.

## Budget awareness

With `budget_aware=True` (default) the agent gets a `floe_budget_status` tool and a budget-aware backstory ("check remaining budget, prefer cheaper paths, stop when near the limit"). These are **soft** signals — useful for finishing on budget. The **hard cap (server-side spend limit + facilitator) is the real protection.**

## API surface

```python
from crewai_floe import (
    budget_enabled_agent,   # factory → plain crewai.Agent
    FloeBudget,             # usd_limit + opt-in allowlist + delegation params
    Floe402Tool,            # per-call paid x402 tool
    FloeLLM,                # crewai.LLM routed through the Floe metered proxy
    get_floe_crewai_tools,  # convert all Floe AgentKit actions to crewai tools
)
```

## How it relates to `floe-agentkit-actions`

This package is a **thin re-export** of `floe_agentkit_actions.integrations.crewai` — the single source of truth. Installing `crewai-floe` pulls in `floe-agentkit-actions` and `crewai`. If you already depend on `floe-agentkit-actions[crewai]`, import directly from `floe_agentkit_actions.integrations.crewai`.

## License

MIT
