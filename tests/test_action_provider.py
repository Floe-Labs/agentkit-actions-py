"""Tests for FloeActionProvider with mocked contract calls."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from floe_agentkit_actions.action_provider import FloeActionProvider
from floe_agentkit_actions.constants import (
    AERODROME_QUOTER_V2_ADDRESS,
    BASE_MAINNET_MATCHER,
    BASE_MAINNET_VIEWS,
)


@pytest.fixture
def provider():
    return FloeActionProvider()


class TestConstructor:
    def test_default_addresses(self, provider: FloeActionProvider):
        assert provider._matcher_address == BASE_MAINNET_MATCHER
        assert provider._views_address == BASE_MAINNET_VIEWS

    def test_custom_config(self):
        from floe_agentkit_actions.types import FloeConfig

        config = FloeConfig(
            lending_intent_matcher_address="0x" + "11" * 20,
            lending_views_address="0x" + "22" * 20,
            known_market_ids=["0x" + "33" * 32],
        )
        p = FloeActionProvider(config)
        assert p._matcher_address == "0x" + "11" * 20
        assert p._views_address == "0x" + "22" * 20
        assert len(p._known_market_ids) == 1


class TestResolveReceiverAddress:
    def test_with_provided_address(self, provider: FloeActionProvider):
        addr = "0x" + "ab" * 20
        assert provider._resolve_receiver_address(addr) == addr

    def test_with_session_address(self, provider: FloeActionProvider):
        addr = "0x" + "cd" * 20
        provider._deployed_receiver_address = addr
        assert provider._resolve_receiver_address(None) == addr

    def test_raises_when_no_address(self, provider: FloeActionProvider):
        with pytest.raises(ValueError, match="No receiver address"):
            provider._resolve_receiver_address(None)


class TestGetFlashLoanFee:
    def test_returns_formatted_fee(self, provider: FloeActionProvider, mock_wallet):
        mock_wallet.mock_read(BASE_MAINNET_MATCHER, "getFlashloanFeeBps", 30)

        result = provider.get_flash_loan_fee(mock_wallet, {})

        assert "Flash Loan Fee" in result
        assert "30" in result
        assert "0.30%" in result
