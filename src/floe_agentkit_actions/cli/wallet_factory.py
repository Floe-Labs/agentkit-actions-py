"""Wallet provider factory — creates private key or CDP wallets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def create_wallet(config: dict[str, Any]) -> Any:
    """Create a wallet provider from config dict.

    Config keys:
        type: "private-key" | "cdp"
        private_key: str (for private-key type)
        rpc_url: str | None (for private-key type)
        api_key_name: str (for cdp type)
        api_key_private_key: str (for cdp type)
    """
    if config["type"] == "private-key":
        return _create_private_key_wallet(config["private_key"], config.get("rpc_url"))
    elif config["type"] == "cdp":
        return _create_cdp_wallet(config["api_key_name"], config["api_key_private_key"])
    else:
        raise ValueError(f"Unknown wallet type: {config['type']}")


def _create_private_key_wallet(private_key: str, rpc_url: str | None = None) -> Any:
    """Create an EvmWalletProvider from a raw private key."""
    from coinbase_agentkit.wallet_providers import EvmWalletProvider

    # Use custom RPC if provided, otherwise default
    provider_config: dict[str, Any] = {
        "private_key": private_key,
        "network_id": "base-mainnet",
    }
    if rpc_url:
        provider_config["rpc_url"] = rpc_url

    return EvmWalletProvider(**provider_config)


def _create_cdp_wallet(api_key_name: str, api_key_private_key: str) -> Any:
    """Create a CdpWalletProvider with MPC key management."""
    from coinbase_agentkit.wallet_providers import CdpWalletProvider

    wallet_data_file = Path.cwd() / ".wallet-data.json"
    saved_wallet_data = None
    if wallet_data_file.exists():
        saved_wallet_data = wallet_data_file.read_text()

    wallet_provider = CdpWalletProvider.configure_with_wallet(
        api_key_name=api_key_name,
        api_key_private_key=api_key_private_key,
        network_id="base-mainnet",
        cdp_wallet_data=saved_wallet_data,
    )

    # Persist wallet data for reuse
    wallet_data = wallet_provider.export_wallet()
    wallet_data_file.write_text(json.dumps(wallet_data))

    return wallet_provider
