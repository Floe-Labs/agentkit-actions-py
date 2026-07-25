"""Standalone usage of FloeActionProvider — no AI framework required.

Run: PRIVATE_KEY=0x... python examples/standalone.py
Requires: PRIVATE_KEY environment variable
"""

from __future__ import annotations

import os


def main() -> None:
    from coinbase_agentkit.wallet_providers import (
        EthAccountWalletProvider,
        EthAccountWalletProviderConfig,
    )
    from eth_account import Account

    from floe_agentkit_actions import FloeConfig, floe_action_provider

    # Create wallet provider
    private_key = os.environ.get("PRIVATE_KEY")
    if not private_key:
        print("Error: Set PRIVATE_KEY environment variable")
        return

    rpc_url = os.environ.get("BASE_RPC_URL")
    wallet_provider = EthAccountWalletProvider(
        EthAccountWalletProviderConfig(
            account=Account.from_key(private_key),
            chain_id="8453",  # Base Mainnet
            rpc_url=rpc_url,
        )
    )

    address = wallet_provider.get_address()
    print(f"Connected: {address}\n")

    # Create Floe action provider
    floe = floe_action_provider(FloeConfig())

    # Example: Get oracle price
    print("--- Oracle Price ---")
    result = floe.get_price(wallet_provider, {
        "collateral_token": "0x4200000000000000000000000000000000000006",  # WETH
        "loan_token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
    })
    print(result)

    # Example: Get my loans
    print("\n--- My Loans ---")
    result = floe.get_my_loans(wallet_provider, {})
    print(result)

    # Example: Check flash loan fee
    print("\n--- Flash Loan Fee ---")
    result = floe.get_flash_loan_fee(wallet_provider, {})
    print(result)


if __name__ == "__main__":
    main()
