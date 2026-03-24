"""Shared test fixtures."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


class MockWalletProvider:
    """Mock EvmWalletProvider for testing without a real blockchain connection."""

    def __init__(self, address: str = "0x1234567890abcdef1234567890abcdef12345678"):
        self._address = address
        self._read_responses: dict[str, Any] = {}
        self._send_responses: list[str] = []

    def get_address(self) -> str:
        return self._address

    def get_name(self) -> str:
        return "mock-wallet"

    def get_network(self) -> Any:
        """Return a mock Network object for AgentKit's @create_action decorator."""
        network = MagicMock()
        network.chain_id = "8453"
        network.network_id = "base-mainnet"
        network.protocol_family = "evm"
        return network

    def read_contract(
        self,
        contract_address: str = "",
        abi: list[dict] | None = None,
        function_name: str = "",
        args: list[Any] | None = None,
    ) -> Any:
        key = f"{contract_address}:{function_name}"
        if key in self._read_responses:
            return self._read_responses[key]
        raise Exception(f"No mock response for {key}")

    def send_transaction(self, transaction: dict[str, Any]) -> str:
        if self._send_responses:
            return self._send_responses.pop(0)
        return "0x" + "ab" * 32

    def wait_for_transaction_receipt(self, tx_hash: str) -> dict[str, Any]:
        return {
            "transactionHash": tx_hash,
            "contractAddress": "0x" + "cd" * 20,
            "status": 1,
        }

    def mock_read(self, contract_address: str, function_name: str, response: Any) -> None:
        """Register a mock response for read_contract."""
        self._read_responses[f"{contract_address}:{function_name}"] = response

    def mock_send(self, tx_hash: str) -> None:
        """Register a mock response for send_transaction."""
        self._send_responses.append(tx_hash)


@pytest.fixture
def mock_wallet() -> MockWalletProvider:
    return MockWalletProvider()


@pytest.fixture
def mock_wallet_with_loan(mock_wallet: MockWalletProvider) -> MockWalletProvider:
    """Wallet with a mock loan #1 configured."""
    from floe_agentkit_actions.constants import BASE_MAINNET_MATCHER

    # Mock getLoan response (as a named tuple-like object)
    loan = MagicMock()
    loan.marketId = b"\x00" * 32
    loan.loanId = 1
    loan.lender = "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    loan.borrower = mock_wallet.get_address()
    loan.loanToken = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC
    loan.collateralToken = "0x4200000000000000000000000000000000000006"  # WETH
    loan.principal = 1000 * 10**6  # 1000 USDC
    loan.interestRateBps = 500  # 5%
    loan.ltvBps = 7000
    loan.liquidationLtvBps = 8500
    loan.marketFeeBps = 100
    loan.matcherCommissionBps = 50
    loan.startTime = 1700000000
    loan.duration = 2592000  # 30 days
    loan.collateralAmount = 10**18  # 1 WETH
    loan.repaid = False
    loan.gracePeriod = 86400
    loan.minInterestBps = 0

    mock_wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", loan)
    mock_wallet.mock_read(BASE_MAINNET_MATCHER, "getCurrentLtvBps", 6500)
    mock_wallet.mock_read(BASE_MAINNET_MATCHER, "isHealthy", True)
    mock_wallet.mock_read(
        BASE_MAINNET_MATCHER, "getAccruedInterest", (5 * 10**6, 86400)
    )

    return mock_wallet
