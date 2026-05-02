# floe-agentkit-actions

[![PyPI version](https://img.shields.io/pypi/v/floe-agentkit-actions)](https://pypi.org/project/floe-agentkit-actions/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB)](https://python.org)
[![Base Mainnet](https://img.shields.io/badge/Base-Mainnet-0052FF)](https://basescan.org/address/0x17946cD3e180f82e632805e5549EC913330Bb175)

Coinbase AgentKit ActionProvider for the [Floe](https://floelabs.xyz) credit protocol on Base. **36 actions** for lending, borrowing, flash loans, and x402 credit delegation.

### 5-Second Example

```python
from floe_agentkit_actions import floe_action_provider

provider = floe_action_provider()
# Borrow, check, repay, rollover -- same 36 actions as TypeScript
```

## Installation

```bash
pip install floe-agentkit-actions

# With CLI support
pip install "floe-agentkit-actions[cli]"

# With LangChain integration
pip install "floe-agentkit-actions[langchain]"
```

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

## Actions (36 total: 30 lending + 6 x402)

### Read Actions (8)

| Action | Description |
|--------|-------------|
| `get_markets` | Get info about Floe lending markets (rates, LTV bounds, pause status) |
| `get_loan` | Get detailed loan information (participants, health, time remaining) |
| `get_my_loans` | Get all loans for the connected wallet (as lender or borrower) |
| `check_loan_health` | Check loan health -- current LTV vs liquidation threshold, buffer % |
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
| `repay_loan` | Repay a loan fully or partially (with slippage protection) |
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
| `instant_borrow` | Borrow USDC instantly -- auto-selects best lender, handles approval + register + match in one call |
| `repay_and_reborrow` | Repay an existing loan and instantly borrow again. If reborrow fails, repayment still succeeds |
| `check_credit_status` | Loan health, balance, accrued interest, time to expiry, early repayment terms |
| `request_credit` | Browse available credit offers -- rates, amounts, durations |
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

## CLI

```bash
floe-agent
```

Interactive AI-powered DeFi agent with support for OpenAI, Claude, and Ollama.

The CLI prompts for:

1. **Wallet provider** -- Private Key (direct) or CDP Wallet (MPC managed)
2. **AI provider** -- OpenAI (GPT-4o), Anthropic (Claude), or Ollama (local)
3. **RPC URL** -- Custom Base Mainnet RPC (recommended for reliability)

## Wallet Providers

| Provider | Use Case |
|----------|----------|
| Private Key (`EvmWalletProvider`) | Development / scripting |
| CDP (`CdpWalletProvider`) | Production agents (MPC) |

## Framework Integrations

### LangChain

```python
from floe_agentkit_actions.integrations.langchain import get_floe_langchain_tools

tools = get_floe_langchain_tools(wallet_provider)
```

### OpenAI Function Calling

```python
from floe_agentkit_actions.integrations.openai_agents import get_floe_openai_tools

tools = get_floe_openai_tools(wallet_provider)
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `PRIVATE_KEY` | Wallet private key (0x...) |
| `CDP_API_KEY_NAME` | Coinbase CDP API key name |
| `CDP_API_KEY_PRIVATE_KEY` | Coinbase CDP API secret |
| `OPENAI_API_KEY` | OpenAI (for CLI) |
| `ANTHROPIC_API_KEY` | Anthropic (for CLI) |
| `BASE_RPC_URL` | Custom Base RPC (recommended) |

## Contract Addresses (Base Mainnet)

| Contract | Address |
|----------|---------|
| LendingIntentMatcher | `0x17946cD3e180f82e632805e5549EC913330Bb175` |
| LendingViews | `0x9101027166bE205105a9E0c68d6F14f21f6c5003` |
| PriceOracle | `0xEA058a06b54dce078567f9aa4dBBE82a100210Cc` |
| Aerodrome SwapRouter | `0xBE6D8f0d05cC4be24d5167a3eF062215bE6D18a5` |
| Aerodrome QuoterV2 | `0x254cF9E1E6e233aa1AC962CB9B05b2cFeAAe15b0` |
| WETH | `0x4200000000000000000000000000000000000006` |

## Development

```bash
pip install -e ".[dev]"
pytest
```

## Links

- [Website](https://floelabs.xyz)
- [Documentation](https://docs.floelabs.xyz)
- [TypeScript counterpart (floe-agent)](https://github.com/floelabs/agentkit-actions)
- [MCP Server (@floelabs/mcp-server)](https://github.com/floelabs/floe-mcp-server)

## License

MIT
