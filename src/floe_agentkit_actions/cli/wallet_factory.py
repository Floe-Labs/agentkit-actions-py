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
        wallet_secret: str | None (for cdp type; falls back to CDP_WALLET_SECRET)
    """
    if config["type"] == "private-key":
        return _create_private_key_wallet(config["private_key"], config.get("rpc_url"))
    elif config["type"] == "cdp":
        return _create_cdp_wallet(
            config["api_key_name"],
            config["api_key_private_key"],
            config.get("wallet_secret"),
        )
    else:
        raise ValueError(f"Unknown wallet type: {config['type']}")


def _create_private_key_wallet(private_key: str, rpc_url: str | None = None) -> Any:
    """Create an EthAccountWalletProvider from a raw private key.

    coinbase-agentkit >= 0.7 made ``EvmWalletProvider`` an ABC; the concrete
    local-key class is ``EthAccountWalletProvider``, configured with an
    eth-account signer + chain id (rpc_url falls back to the chain default).
    """
    from coinbase_agentkit.wallet_providers import (
        EthAccountWalletProvider,
        EthAccountWalletProviderConfig,
    )
    from eth_account import Account

    return EthAccountWalletProvider(
        EthAccountWalletProviderConfig(
            account=Account.from_key(private_key),
            chain_id="8453",  # Base Mainnet — the only network this CLI targets
            rpc_url=rpc_url,
        )
    )


def _create_cdp_wallet(
    api_key_name: str, api_key_private_key: str, wallet_secret: str | None = None
) -> Any:
    """Create a CdpEvmWalletProvider with MPC key management.

    coinbase-agentkit >= 0.7 renamed ``CdpWalletProvider`` to
    ``CdpEvmWalletProvider`` and moved to CDP v2 server wallets: the old
    ``configure_with_wallet``/``export_wallet`` seed-export flow is gone.
    Accounts live server-side, so only the account address is persisted in
    ``.wallet-data.json`` to reattach on the next run. CDP v2 additionally
    requires a wallet secret; when ``wallet_secret`` is None the provider
    falls back to the ``CDP_WALLET_SECRET`` env var.
    """
    from coinbase_agentkit.wallet_providers import (
        CdpEvmWalletProvider,
        CdpEvmWalletProviderConfig,
    )

    wallet_data_file = Path.cwd() / ".wallet-data.json"
    saved_address: str | None = None
    if wallet_data_file.exists():
        try:
            # Legacy v1 seed exports parse fine but carry no "address" key →
            # a fresh server account is created and the file is rewritten.
            saved_address = json.loads(wallet_data_file.read_text()).get("address")
        except (json.JSONDecodeError, AttributeError):
            saved_address = None

    wallet_provider = CdpEvmWalletProvider(
        CdpEvmWalletProviderConfig(
            api_key_id=api_key_name,
            api_key_secret=api_key_private_key,
            wallet_secret=wallet_secret,
            network_id="base-mainnet",
            address=saved_address,
        )
    )

    # Persist the server-wallet address for reuse
    wallet_data_file.write_text(json.dumps({"address": wallet_provider.get_address()}))

    return wallet_provider
