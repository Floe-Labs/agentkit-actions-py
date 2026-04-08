"""Regression tests for X402ActionProvider collateral approval.

Mirrors agentkit-actions (TypeScript) commit 142ae79 which switched the
collateral approval from `borrow_limit * 10` (wrong units — treats USDC
raw as collateral amount) to `MAX_UINT256`. The Python port at
src/floe_agentkit_actions/x402_action_provider.py:260 already has the fix;
these tests lock it down so a future refactor cannot silently reintroduce
the unit-conversion bug.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from eth_utils import function_signature_to_4byte_selector

from floe_agentkit_actions.x402_action_provider import X402ActionProvider, X402Config


MAX_UINT256 = 2**256 - 1

# approve(address,uint256) selector
APPROVE_SELECTOR = function_signature_to_4byte_selector("approve(address,uint256)")


class SpyWallet:
    """Minimal wallet stub that records every send_transaction call."""

    def __init__(self, current_allowance: int = 0) -> None:
        self._address = "0x1111111111111111111111111111111111111111"
        self._allowance = current_allowance
        self.sent: list[dict[str, Any]] = []

    def get_address(self) -> str:
        return self._address

    def get_name(self) -> str:
        return "spy-wallet"

    def get_network(self) -> Any:
        from unittest.mock import MagicMock
        net = MagicMock()
        net.chain_id = "8453"
        net.network_id = "base-mainnet"
        net.protocol_family = "evm"
        return net

    def sign_message(self, _message: str) -> str:
        return "0x" + "ab" * 65

    def read_contract(self, **kwargs: Any) -> Any:
        if kwargs.get("function_name") == "allowance":
            return self._allowance
        raise AssertionError(f"Unexpected read_contract call: {kwargs}")

    def send_transaction(self, transaction: dict[str, Any]) -> str:
        self.sent.append(transaction)
        return "0x" + "cd" * 32

    def wait_for_transaction_receipt(self, tx_hash: str) -> dict[str, Any]:
        return {"transactionHash": tx_hash, "status": 1}


def _decode_approve(data: str) -> tuple[str, int]:
    """Decode approve(address,uint256) calldata. Returns (spender_hex, amount)."""
    raw = bytes.fromhex(data.removeprefix("0x"))
    assert raw[:4] == APPROVE_SELECTOR, f"not an approve() selector: {raw[:4].hex()}"
    # 32-byte padded address + 32-byte uint256
    spender = "0x" + raw[4 + 12 : 4 + 32].hex()
    amount = int.from_bytes(raw[4 + 32 : 4 + 64], "big")
    return spender, amount


def _make_provider() -> X402ActionProvider:
    return X402ActionProvider(X402Config(
        matcher_address="0x17946cD3e180f82e632805e5549EC913330Bb175",
        facilitator_url="https://x402.floe.xyz",
    ))


def _mock_facilitator_fetch(path: str, method: str = "GET", body: Any = None) -> dict[str, Any]:
    """Stub for X402ActionProvider._facilitator_fetch covering the two
    endpoints grant_credit_delegation hits."""
    if "/agents/pre-register" in path:
        return {
            "status": 200,
            "body": {"privyWalletAddress": "0x2222222222222222222222222222222222222222"},
            "headers": {},
        }
    if "/agents/register" in path:
        return {
            "status": 200,
            "body": {
                "privyWalletAddress": "0x2222222222222222222222222222222222222222",
                "creditLimit": "10000000000",
                "apiKey": "test-api-key",
            },
            "headers": {},
        }
    return {"status": 200, "body": {}, "headers": {}}


_BASE_ARGS = {
    "facilitator_address": "0x3333333333333333333333333333333333333333",
    "facilitator_url": "https://x402.floe.xyz",
    "borrow_limit": "10000",
    "max_rate_bps": "1500",
    "expiry_days": "90",
    "collateral_token": "0x4200000000000000000000000000000000000006",
}


def test_approves_max_uint256_when_current_allowance_is_zero() -> None:
    provider = _make_provider()
    wallet = SpyWallet(current_allowance=0)

    with patch.object(provider, "_facilitator_fetch", side_effect=_mock_facilitator_fetch):
        result = provider.grant_credit_delegation(wallet, dict(_BASE_ARGS))

    assert "Error" not in result, f"grant_credit_delegation returned error: {result}"

    # Find the approve() tx — sent to the collateral token, not the matcher.
    approve_txs = [
        tx for tx in wallet.sent
        if tx["to"].lower() == _BASE_ARGS["collateral_token"].lower()
    ]
    assert len(approve_txs) == 1, f"expected exactly 1 approve tx, got {len(approve_txs)}"

    spender, amount = _decode_approve(approve_txs[0]["data"])

    # Spender must be the Floe matcher, not some other address.
    assert spender.lower() == "0x17946cD3e180f82e632805e5549EC913330Bb175".lower()

    # CRITICAL: amount must be MAX_UINT256. If this fails, someone has
    # reintroduced the commit 142ae79 pre-fix bug (borrow_limit * 10 in
    # wrong units).
    assert amount == MAX_UINT256, (
        f"approval amount is {amount}, expected MAX_UINT256 ({MAX_UINT256}). "
        f"Pre-fix buggy value would be {int(_BASE_ARGS['borrow_limit']) * 10} — "
        f"if you see THAT value, the bug has been reintroduced."
    )

    # Explicit guardrail against the pre-fix pattern.
    pre_fix_buggy = int(_BASE_ARGS["borrow_limit"]) * 10
    assert amount != pre_fix_buggy, (
        "collateral approval matches the pre-fix buggy pattern "
        "(borrow_limit * 10 in wrong units). REGRESSION."
    )


def test_skips_approve_when_allowance_already_at_max() -> None:
    """If the existing allowance already covers MAX_UINT256, no redundant
    approve tx should be sent."""
    provider = _make_provider()
    wallet = SpyWallet(current_allowance=MAX_UINT256)

    with patch.object(provider, "_facilitator_fetch", side_effect=_mock_facilitator_fetch):
        provider.grant_credit_delegation(wallet, dict(_BASE_ARGS))

    approve_txs = [
        tx for tx in wallet.sent
        if tx["to"].lower() == _BASE_ARGS["collateral_token"].lower()
    ]
    assert len(approve_txs) == 0, "approve tx should not be sent when allowance already sufficient"
