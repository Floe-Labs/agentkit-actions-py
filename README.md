# floe-agentkit-actions

[![PyPI version](https://img.shields.io/pypi/v/floe-agentkit-actions)](https://pypi.org/project/floe-agentkit-actions/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB)](https://python.org)
[![Base Mainnet](https://img.shields.io/badge/Base-Mainnet-0052FF)](https://basescan.org/address/0x17946cD3e180f82e632805e5549EC913330Bb175)

**The Financial OS for AI Agents — Python SDK.**

Wallet, fiat on/off-ramp, working capital, x402 payments, and portable credit. One SDK. Works with Coinbase AgentKit, LangChain, Claude/Cursor (via MCP), and any framework that speaks HTTP.

`floe-agentkit-actions` is the official Python SDK — an AgentKit `ActionProvider` exposing 45 actions across the full Floe stack, at full parity with the TypeScript [`floe-agent`](https://github.com/Floe-Labs/agentkit-actions) package.

> Package name note: the Python distribution is `floe-agentkit-actions` (TS distribution is `floe-agent`). The action surface is identical.

> **Proof points:** 3,000+ secured working capital lines issued · zero defaults · 13,000+ x402 APIs reachable via the Floe proxy.

---

## The Floe Stack (what this SDK covers)

| # | Component | Status | Backed by |
|---|---|---|---|
| 01 | **Agent Wallet** | `GA` | Any `WalletProvider` (CDP, Privy, Viem) + ERC-8004 identity |
| 02 | **Fiat on-ramp** | `GA` (dashboard-driven) | Coinbase onramp via the [Floe dashboard](https://dev-dashboard.floelabs.xyz). Fiat off-ramp `Preview`. |
| 03 | **Secured working capital** | `GA` | `instant_borrow`, `repay_and_reborrow`, `check_credit_status`, `request_credit`, `manual_match_credit` + 15 lending primitives |
| 04 | **Unsecured working capital** | `Preview` | Receivables + chain-of-thought underwriting — email [hello@floelabs.xyz](mailto:hello@floelabs.xyz) for the design partner program |
| 05 | **x402 payment facilitator** | `GA` | `grant_credit_delegation`, `revoke_credit_delegation`, `check_credit_delegation`, `x402_fetch`, `x402_get_balance`, `x402_get_transactions` |
| 06 | **Credit & trust bureau** | Reader `Beta` · Writer `Preview` | `list_credit_thresholds`, `register_credit_threshold`, `delete_credit_threshold` today. Portable ERC-8004 read API in Beta. |

---

## Framework support

| Framework | Status | How |
|---|---|---|
| Coinbase AgentKit | `GA` | Native — `floe_action_provider()` |
| LangChain | `GA` | `from floe_agentkit_actions.integrations.langchain import get_floe_langchain_tools` |
| OpenAI Agents SDK | `Beta` | `from floe_agentkit_actions.integrations.openai_agents import get_floe_openai_tools` |
| Claude Desktop / Claude Code / Cursor | `GA` | Via [floe-mcp-server](https://github.com/Floe-Labs/floe-mcp-server) |
| CrewAI | `Beta` | Via MCP server (see [floe-examples](https://github.com/Floe-Labs/floe-examples)) |
| ElizaOS | `Preview` | MCP fallback today |
| Plain HTTP / REST | `GA` | Any framework — call the [REST API](https://floe-labs.gitbook.io/docs/developers/credit-api) |

---

## Installation

```bash
pip install floe-agentkit-actions

# With CLI support
pip install "floe-agentkit-actions[cli]"

# With LangChain integration
pip install "floe-agentkit-actions[langchain]"
```

> **Fund with fiat:** Agents (or their operators) can fund wallets with USDC via Coinbase — credit card, bank transfer, Apple Pay, Google Pay — directly from the [Floe dashboard](https://dev-dashboard.floelabs.xyz). No crypto on-ramp needed.

### 30-second example

```python
from floe_agentkit_actions import floe_action_provider

provider = floe_action_provider()

# Borrow against on-chain collateral
loan = provider.instant_borrow(wallet_provider, {
    "borrow_amount": "9500000000",
    "collateral_amount": "10000000000",
    "max_interest_rate_bps": "800",
    "duration": "1209600",
})

# Pay any x402 API through the Floe facilitator
response = provider.x402_fetch(wallet_provider, {
    "url": "https://api.example.com/premium",
    "method": "POST",
    "body": {"prompt": "..."},
})

# Check health, then repay
provider.check_credit_status(wallet_provider, {"loan_id": loan["loan_id"]})
provider.repay_loan(wallet_provider, {"loan_id": loan["loan_id"]})
```

---

## Quick Start

### As an AgentKit Provider

```python
from coinbase_agentkit import AgentKit, AgentKitConfig
from floe_agentkit_actions import floe_action_provider

agentkit = AgentKit(AgentKitConfig(
    wallet_provider=wallet_provider,
    action_providers=[floe_action_provider()],
))
```

### Standalone Usage

```python
from floe_agentkit_actions import floe_action_provider

floe = floe_action_provider()
result = floe.get_price(wallet_provider, {
    "collateral_token": "0x4200000000000000000000000000000000000006",  # WETH
    "loan_token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",      # USDC
})
print(result)
```

---

## Actions (45 total)

### Read Actions (8)

| Action | Description |
|--------|-------------|
| `get_markets` | Get info about Floe lending markets (rates, LTV bounds, pause status) |
| `get_loan` | Get detailed loan information (participants, health, time remaining) |
| `get_my_loans` | Get all loans for the connected wallet (as lender or borrower) |
| `check_loan_health` | Check loan health — current LTV vs liquidation threshold, buffer % |
| `get_price` | Get oracle price for a collateral/loan token pair (Chainlink + Pyth) |
| `get_accrued_interest` | Get interest accrued on a loan (amount, time elapsed, rate) |
| `get_liquidation_quote` | Get profit/loss breakdown for liquidating an unhealthy loan |
| `get_intent_book` | Look up an on-chain lend or borrow intent by hash |

### Write Actions (7)

| Action | Description |
|--------|-------------|
| `post_lend_intent` | Post a fixed-rate lending offer (auto-approves loan token) |
| `post_borrow_intent` | Post a borrow request with collateral (auto-approves collateral) |
| `match_intents` | Match a lend + borrow intent to create a loan |
| `repay_loan` | Repay a loan fully or partially (with slippage protection). Collateral auto-returns in the same tx. |
| `add_collateral` | Add collateral to improve loan health |
| `withdraw_collateral` | Withdraw excess collateral (enforces safety buffer) |
| `liquidate_loan` | Liquidate an unhealthy loan (currentLTV >= threshold or overdue) |

All write actions **auto-approve** tokens to the LendingIntentMatcher with a 1% buffer before submitting. Repay and liquidate actions include configurable slippage protection (default 5%).

### Flash Loan Actions (5)

| Action | Description |
|--------|-------------|
| `get_flash_loan_fee` | Get the protocol's flash loan fee (in bps) |
| `estimate_flash_arb_profit` | Simulate a multi-leg arb route via Aerodrome QuoterV2 |
| `flash_loan` | Execute a raw flash loan (receiver must implement `IFlashloanReceiver`) |
| `flash_arb` | Execute a flash arb via a deployed FlashArbReceiver |
| `get_flash_arb_balance` | Check accumulated profit in a FlashArbReceiver |

### Credit Facility Actions (5)

| Action | Description |
|--------|-------------|
| `instant_borrow` | Borrow USDC instantly — auto-selects best lender, handles approval + register + match in one call |
| `repay_and_reborrow` | Repay an existing loan and instantly borrow again. If reborrow fails, repayment still succeeds |
| `check_credit_status` | Loan health, balance, accrued interest, time to expiry, early repayment terms |
| `request_credit` | Browse available credit offers — rates, amounts, durations |
| `manual_match_credit` | Match with a specific lend intent (register + match) |

### Deploy / Verify / Readiness Actions (3)

| Action | Description |
|--------|-------------|
| `deploy_flash_arb_receiver` | Deploy a new FlashArbReceiver with pre-flight checks |
| `check_flash_arb_readiness` | Check environment readiness (fee, liquidity, oracle, router) |
| `verify_flash_arb_receiver` | Verify a receiver's owner and immutable config |

### x402 Credit Delegation Actions (6)

| Action | Description |
|--------|-------------|
| `grant_credit_delegation` | Delegate borrowing authority to a facilitator (sets operator + collateral approval) |
| `revoke_credit_delegation` | Revoke a facilitator's borrowing authority |
| `check_credit_delegation` | Check delegation status (approved, limits, borrowed, expiry) |
| `x402_fetch` | Fetch a URL with automatic x402 payment handling |
| `x402_get_balance` | Check x402 credit balance |
| `x402_get_transactions` | List recent x402 payment transactions |

### Agent Awareness Actions (9)

Lets an agent answer "do I have credit?", "is this call worth it?", and "where am I in the loan lifecycle?" before committing capital. All require `facilitator_api_key` to be configured on the provider.

| Action | Description |
|--------|-------------|
| `get_credit_remaining` | Available USDC, headroom to auto-borrow, utilization in bps, session-cap state |
| `get_loan_state` | Coarse state machine: `idle` \| `borrowing` \| `at_limit` \| `repaying` |
| `get_spend_limit` | Currently active session spend cap, if any |
| `set_spend_limit` | Set a session-level USDC ceiling (resets the session window) |
| `clear_spend_limit` | Remove the session spend cap |
| `list_credit_thresholds` | List registered credit-utilization webhook triggers |
| `register_credit_threshold` | Register a webhook trigger at a utilization threshold (cap: 20 per agent) |
| `delete_credit_threshold` | Remove a registered threshold |
| `estimate_x402_cost` | Preflight an x402 URL — returns cost + reflection against your credit (no payment) |

> **Decision-loop pattern:** call `estimate_x402_cost` → check `willExceedAvailable` / `willExceedSpendLimit` → conditionally `x402_fetch`. This is the "answer the 3 rational-agent questions in one round-trip" workflow.

---

## CLI

```bash
floe-agent
```

Interactive AI-powered agent with support for OpenAI, Claude, and Ollama.

The CLI prompts for:

1. **Wallet provider** — Private Key (direct) or CDP Wallet (MPC managed)
2. **AI provider** — OpenAI (GPT-4o), Anthropic (Claude), or Ollama (local)
3. **RPC URL** — Custom Base Mainnet RPC (recommended for reliability)

---

## Wallet Providers

| Provider | Use Case |
|----------|----------|
| Private Key (`EvmWalletProvider`) | Development / scripting |
| CDP (`CdpWalletProvider`) | Production agents (MPC) |

---

## Framework Integrations

### LangChain (`GA`)

```python
from floe_agentkit_actions.integrations.langchain import get_floe_langchain_tools

tools = get_floe_langchain_tools(wallet_provider)
```

### OpenAI Agents / function calling (`Beta`)

```python
from floe_agentkit_actions.integrations.openai_agents import get_floe_openai_tools

tools = get_floe_openai_tools(wallet_provider)
```

### MCP (Claude Desktop / Claude Code / Cursor) — `GA`

Zero install via the hosted endpoint:

```json
{
  "mcpServers": {
    "floe": {
      "url": "https://mcp.floelabs.xyz/mcp",
      "transport": "streamable-http"
    }
  }
}
```

Or run a local MCP server — see [floe-mcp-server](https://github.com/Floe-Labs/floe-mcp-server).

### CrewAI (`Beta`)

CrewAI agents consume the Floe stack via MCP today. A runnable crew is available in [floe-examples/crewai-demo](https://github.com/Floe-Labs/floe-examples).

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PRIVATE_KEY` | Wallet private key (0x...) |
| `CDP_API_KEY_NAME` | Coinbase CDP API key name |
| `CDP_API_KEY_PRIVATE_KEY` | Coinbase CDP API secret |
| `OPENAI_API_KEY` | OpenAI (for CLI) |
| `ANTHROPIC_API_KEY` | Anthropic (for CLI) |
| `BASE_RPC_URL` | Custom Base RPC (recommended) |
| `FLOE_FACILITATOR_API_KEY` | Required for x402 + agent-awareness actions |

---

## Contract Addresses (Base Mainnet)

| Contract | Address |
|----------|---------|
| LendingIntentMatcher | `0x17946cD3e180f82e632805e5549EC913330Bb175` |
| LendingViews | `0x9101027166bE205105a9E0c68d6F14f21f6c5003` |
| PriceOracle | `0xEA058a06b54dce078567f9aa4dBBE82a100210Cc` |
| x402 Facilitator | `0x58EDdE022FFDAD3Fb0Fb0E7D51eb05AaF66a31f1` |
| Aerodrome SwapRouter | `0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5` |
| Aerodrome QuoterV2 | `0x254cF9E1E6e233aa1AC962CB9B05b2cFeAAe15b0` |
| WETH | `0x4200000000000000000000000000000000000006` |

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

---

## Links

- [Website](https://floelabs.xyz)
- [Documentation](https://floe-labs.gitbook.io/docs)
- [TypeScript counterpart (`floe-agent`)](https://github.com/Floe-Labs/agentkit-actions)
- [MCP server (`@floelabs/mcp-server`)](https://github.com/Floe-Labs/floe-mcp-server)
- [End-to-end examples](https://github.com/Floe-Labs/floe-examples)

## License

MIT
