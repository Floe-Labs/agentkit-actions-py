"""Wallet-factory regression tests.

`cli/wallet_factory.py` was silently broken by the coinbase-agentkit 0.7
API change (`EvmWalletProvider` became an ABC, `CdpWalletProvider` was
renamed to `CdpEvmWalletProvider`). These tests pin the factory to the
current concrete classes so a future AgentKit rename fails loudly in CI
instead of at REPL startup.
"""

from __future__ import annotations

import pytest
from eth_account import Account

from floe_agentkit_actions.cli.wallet_factory import create_wallet

# Deterministic throwaway key — never funded, never used on-chain.
TEST_PRIVATE_KEY = "0x" + "11" * 32


def test_private_key_wallet_uses_eth_account_provider() -> None:
    from coinbase_agentkit.wallet_providers import EthAccountWalletProvider

    wallet = create_wallet(
        {
            "type": "private-key",
            "private_key": TEST_PRIVATE_KEY,
            "rpc_url": "https://mainnet.base.org",
        }
    )
    assert isinstance(wallet, EthAccountWalletProvider)
    assert wallet.get_address() == Account.from_key(TEST_PRIVATE_KEY).address
    assert wallet.get_network().chain_id == "8453"


def test_private_key_wallet_defaults_rpc_url() -> None:
    # No rpc_url → the provider falls back to the chain default; construction
    # must not require one (no network I/O happens at init).
    wallet = create_wallet({"type": "private-key", "private_key": TEST_PRIVATE_KEY})
    assert wallet.get_address() == Account.from_key(TEST_PRIVATE_KEY).address


def test_cdp_wallet_maps_to_cdp_evm_provider(monkeypatch, tmp_path) -> None:
    """The CDP path must target CdpEvmWalletProvider with v2 config fields."""
    import coinbase_agentkit.wallet_providers as wp

    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, config: wp.CdpEvmWalletProviderConfig) -> None:
            captured["config"] = config

        def get_address(self) -> str:
            return "0x" + "22" * 20

    monkeypatch.setattr(wp, "CdpEvmWalletProvider", FakeProvider)
    monkeypatch.chdir(tmp_path)  # .wallet-data.json is written to cwd

    wallet = create_wallet(
        {
            "type": "cdp",
            "api_key_name": "key-id",
            "api_key_private_key": "key-secret",
            "wallet_secret": "wallet-secret",
        }
    )
    config = captured["config"]
    assert isinstance(config, wp.CdpEvmWalletProviderConfig)
    assert config.api_key_id == "key-id"
    assert config.api_key_secret == "key-secret"
    assert config.wallet_secret == "wallet-secret"
    assert config.network_id == "base-mainnet"
    assert config.address is None
    assert wallet.get_address() == "0x" + "22" * 20
    # The persisted address must be picked back up on the next run.
    wallet2 = create_wallet(
        {
            "type": "cdp",
            "api_key_name": "key-id",
            "api_key_private_key": "key-secret",
            "wallet_secret": "wallet-secret",
        }
    )
    assert captured["config"].address == wallet2.get_address()


def test_cdp_wallet_secret_falls_back_to_env(monkeypatch, tmp_path) -> None:
    """Omitting wallet_secret leaves the provider to read CDP_WALLET_SECRET."""
    import coinbase_agentkit.wallet_providers as wp

    captured: dict[str, object] = {}

    class FakeProvider:
        def __init__(self, config: wp.CdpEvmWalletProviderConfig) -> None:
            captured["config"] = config

        def get_address(self) -> str:
            return "0x" + "33" * 20

    monkeypatch.setattr(wp, "CdpEvmWalletProvider", FakeProvider)
    monkeypatch.chdir(tmp_path)

    create_wallet(
        {
            "type": "cdp",
            "api_key_name": "key-id",
            "api_key_private_key": "key-secret",
        }
    )
    assert captured["config"].wallet_secret is None


def test_unknown_wallet_type_raises() -> None:
    with pytest.raises(ValueError, match="Unknown wallet type"):
        create_wallet({"type": "ledger"})
