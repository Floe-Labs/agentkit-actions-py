"""Regression test: onBehalfOf field is forwarded into the BorrowIntent
struct that gets encoded and sent to registerBorrowIntent.

Mirrors agentkit-actions (TypeScript) commit b69b483 which threaded
onBehalfOf through instant_borrow / repay_and_reborrow / post_borrow_intent.

NOTE: The Python port only has `post_borrow_intent` exposed today — it is
MISSING `instant_borrow` and `repay_and_reborrow` which the TS port has.
See test_action_count.py for the full parity gap. This file only covers
what exists.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from floe_agentkit_actions.action_provider import FloeActionProvider
from floe_agentkit_actions.constants import BASE_MAINNET_MATCHER


class SpyWallet:
    """Wallet stub that captures send_transaction calls for inspection."""

    def __init__(self) -> None:
        self._address = "0x1111111111111111111111111111111111111111"
        self.sent: list[dict[str, Any]] = []

    def get_address(self) -> str:
        return self._address

    def get_name(self) -> str:
        return "spy-wallet"

    def get_network(self) -> Any:
        net = MagicMock()
        net.chain_id = "8453"
        net.network_id = "base-mainnet"
        net.protocol_family = "evm"
        return net

    def read_contract(self, **kwargs: Any) -> Any:
        fn = kwargs.get("function_name")
        if fn == "allowance":
            # Return a high allowance so approve is skipped — we're not
            # testing approval here.
            return 2**256 - 1
        if fn == "getMarket":
            market = MagicMock()
            market.loanToken = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC
            market.collateralToken = "0x4200000000000000000000000000000000000006"  # WETH
            return market
        raise AssertionError(f"Unexpected read_contract: {fn}")

    def send_transaction(self, transaction: dict[str, Any]) -> str:
        self.sent.append(transaction)
        return "0x" + "cd" * 32


_BORROW_ARGS = {
    "market_id": "0xfe92656527bae8e6d37a9e0bb785383fbb33f1f0c7e29fdd733f5af7390c2930",
    "borrow_amount": "1000000000",
    "collateral_amount": "1000000000000000000",
    "min_fill_amount": "500000000",
    "max_interest_rate_bps": "1500",
    "min_ltv_bps": "5000",
    "min_duration": "2592000",
    "max_duration": "2592000",
    "allow_partial_fill": False,
    "matcher_commission_bps": "50",
    "expiry_seconds": "86400",
}


def _find_register_borrow_tx(txs: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the (single) matcher tx from the captured sends.

    In the test setup, allowance is pre-seeded so _ensure_allowance() is a
    no-op, meaning the ONLY tx sent is registerBorrowIntent."""
    matcher_txs = [tx for tx in txs if tx["to"].lower() == BASE_MAINNET_MATCHER.lower()]
    assert len(matcher_txs) == 1, (
        f"expected exactly 1 matcher tx (registerBorrowIntent), got {len(matcher_txs)}"
    )
    return matcher_txs[0]


def _extract_on_behalf_of_from_calldata(data: str) -> str:
    """Decode the `onBehalfOf` field from a registerBorrowIntent calldata.

    Dynamic-tuple ABI encoding: selector(4) + head_pointer(32) -> then the
    tuple data starts at offset 4+32 = 36. The BorrowIntent tuple begins
    with (borrower, onBehalfOf, ...) — both 32-byte padded addresses. So
    the onBehalfOf address occupies raw[36+32 : 36+64] and the actual
    20-byte address is the last 20 bytes of that slot.
    """
    raw = bytes.fromhex(data.removeprefix("0x"))
    tuple_start = 4 + 32
    on_behalf_of_slot = raw[tuple_start + 32 : tuple_start + 64]
    return "0x" + on_behalf_of_slot[12:].hex()


def test_post_borrow_intent_defaults_on_behalf_of_to_user_address() -> None:
    provider = FloeActionProvider()
    wallet = SpyWallet()
    args = dict(_BORROW_ARGS)
    # on_behalf_of omitted — should default to user_address

    result = provider.post_borrow_intent(wallet, args)
    assert "Error" not in result, f"post_borrow_intent returned error: {result}"

    tx = _find_register_borrow_tx(wallet.sent)
    on_behalf_of = _extract_on_behalf_of_from_calldata(tx["data"])
    assert on_behalf_of.lower() == wallet.get_address().lower(), (
        f"onBehalfOf should default to user_address but got {on_behalf_of}"
    )


def test_post_borrow_intent_forwards_explicit_on_behalf_of() -> None:
    provider = FloeActionProvider()
    wallet = SpyWallet()
    explicit = "0x9999999999999999999999999999999999999999"
    args = dict(_BORROW_ARGS)
    args["on_behalf_of"] = explicit

    result = provider.post_borrow_intent(wallet, args)
    assert "Error" not in result, f"post_borrow_intent returned error: {result}"

    tx = _find_register_borrow_tx(wallet.sent)
    on_behalf_of = _extract_on_behalf_of_from_calldata(tx["data"])
    assert on_behalf_of.lower() == explicit.lower(), (
        f"onBehalfOf should be the explicit override but got {on_behalf_of}"
    )
