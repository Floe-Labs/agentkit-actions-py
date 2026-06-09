# floe-agentkit-actions

[![PyPI version](https://img.shields.io/pypi/v/floe-agentkit-actions)](https://pypi.org/project/floe-agentkit-actions/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB)](https://python.org)
[![Base Mainnet](https://img.shields.io/badge/Base-Mainnet-0052FF)](https://basescan.org/address/0x17946cD3e180f82e632805e5549EC913330Bb175)

**The spend layer for AI agents — Python SDK.** Pay any of 2,000+ vendor APIs
through one endpoint, with budgets your agent can reason about. Walletless. No
crypto required.

[Website](https://floelabs.xyz) · [Docs](https://floe-labs.gitbook.io/docs) · [Dashboard](https://dev-dashboard.floelabs.xyz) · [TypeScript SDK](https://github.com/Floe-Labs/agentkit-actions)

---

## What it does

Your agent calls paid APIs — LLMs, voice, search, data. Floe is the spend layer
in front of those calls:

- **One endpoint, many vendors.** Pay any x402 API through the Floe facilitator. No per-vendor accounts or keys.
- **Budgets the agent can reason about.** Ask "do I have budget? is this call worth it?" *before* paying — not after.
- **Programmable spend controls.** Per-call caps, daily limits, allowed destinations, session ceilings — enforced server-side, before money moves.
- **Walletless.** Email + a funding source. Floe provisions wallets in the background — no MetaMask, no seed phrase, no gas. The stablecoin rails are invisible.
- **Real-time visibility.** Every call is a typed receipt: target, amount, status, time. Reconcile, alert, or revoke from the dashboard.

Two layers in one package:

| Layer | What it is | For |
|---|---|---|
| **`FloeAgent`** ⭐ | High-level runtime client. No wallet, no chain knowledge. Dollars in, dollars out (`agent.fetch(url)`, `agent.balance()`). | Most agent developers |
| **Action providers** | 47 AgentKit actions for self-custody, lending, and framework integrations — full parity with the TypeScript `floe-agent` package. | Self-custody / on-chain use cases |

> **Package name:** the Python distribution is `floe-agentkit-actions` (the TypeScript distribution is `floe-agent`). The action surface is identical.

> **$2 free credit (~200 API calls).** Your agent can start paying for APIs today — no card required. [Get started →](https://dev-dashboard.floelabs.xyz)

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

## Install

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
import os
from coinbase_agentkit.wallet_providers import EvmWalletProvider
from floe_agentkit_actions import floe_action_provider

# 1. Wallet provider (use any CDP/Privy/Viem provider; private key shown for brevity)
wallet_provider = EvmWalletProvider.from_private_key(
    private_key=os.environ["PRIVATE_KEY"],
    rpc_url=os.environ["BASE_RPC_URL"],
    network_id="base-mainnet",
)

provider = floe_action_provider()

# 2. Borrow against on-chain collateral. Actions return formatted strings
#    intended for an LLM — print them or pass them to your agent.
print(provider.instant_borrow(wallet_provider, {
    "borrow_amount": "9500000000",
    "collateral_amount": "10000000000",
    "max_interest_rate_bps": "800",
    "duration": "1209600",
}))

# 3. Pay any x402 API through the Floe facilitator
print(provider.x402_fetch(wallet_provider, {
    "url": "https://api.example.com/premium",
    "method": "POST",
    "body": {"prompt": "..."},
}))

# 4. To act on a specific loan, fetch the id from get_my_loans / get_loan and
#    pass it through (here "42" is a placeholder):
print(provider.check_credit_status(wallet_provider, {"loan_id": "42"}))
print(provider.repay_loan(wallet_provider, {"loan_id": "42"}))
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

## Actions (47 total)

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

> **Return shape:** action methods return formatted strings designed for AgentKit / LLM consumption — not parsed dicts. To extract a `loan_id`, call `get_my_loans` (or use the loan id surfaced by the LLM through `AgentKit.run(...)`).

### Flash Loan Actions (5)

| Action | Description |
|--------|-------------|
| `get_flash_loan_fee` | Get the protocol's flash loan fee (in bps) |
| `estimate_flash_arb_profit` | Simulate a multi-leg arb route via Aerodrome QuoterV2 |
| `flash_loan` | Execute a raw flash loan (receiver must implement `IFlashloanReceiver`) |
| `flash_arb` | Execute a flash arb via a deployed FlashArbReceiver |
| `get_flash_arb_balance` | Check accumulated profit in a FlashArbReceiver |

### Credit Facility Actions (7)

| Action | Description |
|--------|-------------|
| `instant_borrow` | Borrow USDC instantly — auto-selects best lender, handles approval + register + match in one call |
| `repay_and_reborrow` | Repay an existing loan and instantly borrow again. If reborrow fails, repayment still succeeds |
| `repay_credit` | Repay an open credit line (full or partial) |
| `renew_credit_line` | Roll an existing credit line into a fresh term |
| `check_credit_status` | Loan health, balance, accrued interest, time to expiry, early repayment terms |
| `request_credit` | Browse available credit offers — rates, amounts, durations |
| `manual_match_credit` | Match with a specific lend intent (register + match) |

### Deploy / Verify / Readiness Actions (3)

| Action | Description |
|--------|-------------|
| `deploy_flash_arb_receiver` | Deploy a new FlashArbReceiver with pre-flight checks |
| `check_flash_arb_readiness` | Check environment readiness (fee, liquidity, oracle, router) |
| `verify_flash_arb_receiver` | Verify a receiver's owner and immutable config |

### x402 Credit Delegation Actions (8)

| Action | Description |
|--------|-------------|
| `grant_credit_delegation` | Delegate borrowing authority to a facilitator (sets operator + collateral approval) |
| `open_credit_line` | Open a USDC/USDC credit line for the delegated agent |
| `revoke_credit_delegation` | Revoke a facilitator's borrowing authority |
| `check_credit_delegation` | Check delegation status (approved, limits, borrowed, expiry) |
| `x402_fetch` | Fetch a URL with automatic x402 payment handling |
| `x402_get_balance` | Check x402 credit balance |
| `x402_await_settlement` | Poll a pending x402 reservation until it settles |
| `x402_get_transactions` | List recent x402 payment transactions |

### Agent Awareness Actions (9)

Lets an agent answer "do I have credit?", "is this call worth it?", and "where am I in the loan lifecycle?" before committing capital. All require a facilitator API key to be configured on the provider via `X402Config(facilitator_api_key=...)`.

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

> **Decision-loop pattern:** call `estimate_x402_cost` → inspect the returned string for `willExceedAvailable` / `willExceedSpendLimit` → conditionally `x402_fetch`. This is the "answer the 3 rational-agent questions in one round-trip" workflow.

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

## Configuring the x402 facilitator key

The x402 and agent-awareness actions require a facilitator API key. The SDK does **not** read any env var directly — pass the key through `X402Config` (or the equivalent option on `floe_action_provider`):

```python
from floe_agentkit_actions import floe_action_provider
from floe_agentkit_actions.x402 import X402Config
import os

provider = floe_action_provider(
    x402_config=X402Config(facilitator_api_key=os.environ["FLOE_FACILITATOR_API_KEY"]),
)
```

A common convention is to store the value in a `FLOE_FACILITATOR_API_KEY` env var in your app and forward it to the SDK as shown.

---

## Environment Variables

These are read by the CLI and the examples in this repo. The SDK itself does **not** read env vars — see *Configuring the x402 facilitator key* above for facilitator auth.

| Variable | Used by | Description |
|----------|---------|-------------|
| `PRIVATE_KEY` | CLI / examples | Wallet private key (0x...) |
| `CDP_API_KEY_NAME` | CLI | Coinbase CDP API key name |
| `CDP_API_KEY_PRIVATE_KEY` | CLI | Coinbase CDP API secret |
| `OPENAI_API_KEY` | CLI | OpenAI key for the conversational agent |
| `ANTHROPIC_API_KEY` | CLI | Anthropic key for the conversational agent |
| `BASE_RPC_URL` | CLI / examples | Custom Base RPC (recommended) |
| `FLOE_FACILITATOR_API_KEY` | Your app | Convention only — forward to `X402Config(facilitator_api_key=...)` |

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
