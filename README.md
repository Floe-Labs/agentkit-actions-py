# floe-agentkit-actions

Coinbase AgentKit ActionProvider for the **Floe DeFi lending protocol** on Base.

23 AI-agent actions for intent-based lending, flash loan arbitrage, and loan management.

## Installation

```bash
pip install floe-agentkit-actions

# With CLI support
pip install floe-agentkit-actions[cli]

# With LangChain integration
pip install floe-agentkit-actions[langchain]
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

### CLI

```bash
floe-agent
```

Interactive AI-powered DeFi agent with support for OpenAI, Claude, and Ollama.

## Actions (23)

### Read (8)
- `get_markets` — Query lending market info
- `get_loan` — Get loan details
- `get_my_loans` — All loans for connected wallet
- `check_loan_health` — Health status and liquidation distance
- `get_price` — Oracle price (Chainlink + Pyth)
- `get_accrued_interest` — Interest accrued on a loan
- `get_liquidation_quote` — Liquidation profit/loss breakdown
- `get_intent_book` — Look up on-chain intent by hash

### Write (7)
- `post_lend_intent` — Post lending offer at fixed rate
- `post_borrow_intent` — Post borrow request with collateral
- `match_intents` — Match lend + borrow intents to create loan
- `repay_loan` — Fully or partially repay loan
- `add_collateral` — Add collateral to improve health
- `withdraw_collateral` — Withdraw excess collateral
- `liquidate_loan` — Liquidate unhealthy loan

### Flash Loans (5)
- `get_flash_loan_fee` — Query protocol flash fee
- `estimate_flash_arb_profit` — Simulate multi-leg arb profit
- `flash_loan` — Raw flash loan (receiver must be smart contract)
- `flash_arb` — Flash arb via FlashArbReceiver
- `get_flash_arb_balance` — Check accumulated profit

### Deploy (3)
- `deploy_flash_arb_receiver` — Deploy FlashArbReceiver with pre-flight checks
- `check_flash_arb_readiness` — Environment readiness check
- `verify_flash_arb_receiver` — Validate receiver configuration

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

```bash
PRIVATE_KEY=0x...               # Wallet private key
CDP_API_KEY_NAME=...            # Coinbase CDP API key name
CDP_API_KEY_PRIVATE_KEY=...     # Coinbase CDP API secret
OPENAI_API_KEY=sk-...           # OpenAI (for CLI)
ANTHROPIC_API_KEY=sk-ant-...    # Anthropic (for CLI)
BASE_RPC_URL=https://...        # Custom Base RPC (recommended)
```

## Development

```bash
pip install -e ".[dev]"
pytest
```

## License

MIT
