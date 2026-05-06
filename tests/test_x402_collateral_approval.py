"""Regression tests for X402ActionProvider collateral approval.

The approval surface for ``grant_credit_delegation`` has two history bugs
worth locking down:

1. **Wrong-units bug (commit 142ae79):** approving ``borrow_limit * 10``
   treated USDC raw units as collateral units. Fixed by switching to
   MAX_UINT256.
2. **Silent-infinite default:** MAX_UINT256 was then granted on every
   delegation by default, with no way for callers to bound it. Fixed by
   requiring callers to opt in explicitly to one of two paths:
   ``unsafe_infinite_approval=True`` (preserves old behavior) or
   ``collateral_approval=<raw>`` (bounded). Neither set → no approve tx
   is sent and the response reports the current matcher allowance so
   the caller can decide whether to grant one externally via their
   wallet provider.

These tests pin the post-fix contract for all three branches plus the
mutually-exclusive rejection case.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from eth_utils import function_signature_to_4byte_selector

from floe_agentkit_actions.x402_action_provider import (
    GrantCreditDelegationSchema,
    X402ActionProvider,
    X402Config,
)

MAX_UINT256 = 2**256 - 1
MATCHER_ADDRESS = "0x17946cD3e180f82e632805e5549EC913330Bb175"

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
    spender = "0x" + raw[4 + 12 : 4 + 32].hex()
    amount = int.from_bytes(raw[4 + 32 : 4 + 64], "big")
    return spender, amount


def _approve_txs(wallet: SpyWallet, collateral_token: str) -> list[dict[str, Any]]:
    """Filter sent txs down to those targeting the collateral token."""
    return [tx for tx in wallet.sent if tx["to"].lower() == collateral_token.lower()]


def _make_provider() -> X402ActionProvider:
    return X402ActionProvider(
        X402Config(
            matcher_address=MATCHER_ADDRESS,
            facilitator_url="https://x402.floe.xyz",
        )
    )


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


# ────────────────────────────────────────────────────────────────────────────
# Default behavior: neither flag set → no approve tx; current matcher
# allowance is read and rendered back to the caller.
# ────────────────────────────────────────────────────────────────────────────


def test_default_neither_flag_set_skips_approve_and_reports_current_allowance() -> None:
    """Default behavior change: omitting both approval flags now skips the
    approve tx entirely. The response reports the current matcher allowance
    so the caller knows whether their wallet is already bounded or still
    has a stale infinite grant from the old default — replacing the prior
    misleading 'facilitator-initiated borrows will fail' wording, which was
    inaccurate when a leftover MAX_UINT256 grant was still active."""
    provider = _make_provider()
    args = dict(_BASE_ARGS)

    # Case 1: clean wallet — allowance reads as 0 and is rendered.
    clean_wallet = SpyWallet(current_allowance=0)
    with patch.object(provider, "_facilitator_fetch", side_effect=_mock_facilitator_fetch):
        clean_result = provider.grant_credit_delegation(clean_wallet, args)

    assert "Error" not in clean_result, f"unexpected error: {clean_result}"
    assert _approve_txs(clean_wallet, _BASE_ARGS["collateral_token"]) == [], (
        "expected zero approve txs when neither flag is set; "
        "if you see one, the silent-infinite default has been reintroduced"
    )
    assert "No approval tx was sent" in clean_result, f"expected new no-approval messaging, got:\n{clean_result}"
    # Allowance is rendered back as raw 0 so the caller knows the matcher
    # has no allowance and they need to grant one.
    assert "Current matcher allowance on this token (raw): 0" in clean_result
    # Old misleading wording must be gone.
    assert "NOT SET" not in clean_result
    assert "approve_token" not in clean_result
    assert "borrows will fail" not in clean_result

    # Case 2: stale MAX_UINT256 grant from the old default — still no tx,
    # but the response makes the leftover allowance visible to the caller.
    stale_wallet = SpyWallet(current_allowance=MAX_UINT256)
    with patch.object(provider, "_facilitator_fetch", side_effect=_mock_facilitator_fetch):
        stale_result = provider.grant_credit_delegation(stale_wallet, args)

    assert "Error" not in stale_result, f"unexpected error: {stale_result}"
    assert _approve_txs(stale_wallet, _BASE_ARGS["collateral_token"]) == []
    assert "No approval tx was sent" in stale_result
    assert str(MAX_UINT256) in stale_result, "expected leftover MAX_UINT256 allowance to be rendered in the response"
    assert "borrows will fail" not in stale_result


# ────────────────────────────────────────────────────────────────────────────
# unsafe_infinite_approval=True → MAX_UINT256 (preserves pre-fix behavior)
# ────────────────────────────────────────────────────────────────────────────


def test_unsafe_infinite_approval_grants_max_uint256() -> None:
    provider = _make_provider()
    wallet = SpyWallet(current_allowance=0)

    args = dict(_BASE_ARGS, unsafe_infinite_approval=True)
    with patch.object(provider, "_facilitator_fetch", side_effect=_mock_facilitator_fetch):
        result = provider.grant_credit_delegation(wallet, args)

    assert "Error" not in result, f"unexpected error: {result}"

    approves = _approve_txs(wallet, _BASE_ARGS["collateral_token"])
    assert len(approves) == 1, f"expected exactly 1 approve tx, got {len(approves)}"

    spender, amount = _decode_approve(approves[0]["data"])
    assert spender.lower() == MATCHER_ADDRESS.lower(), f"approve spender must be the matcher; got {spender}"

    # CRITICAL: the unsafe-infinite path must approve the literal MAX_UINT256.
    # The pre-fix wrong-units bug used `borrow_limit * 10` here — guard against
    # it being silently reintroduced.
    assert amount == MAX_UINT256, (
        f"approval amount is {amount}, expected MAX_UINT256 ({MAX_UINT256}). "
        f"Pre-fix wrong-units value would be {int(_BASE_ARGS['borrow_limit']) * 10}."
    )
    pre_fix_buggy = int(_BASE_ARGS["borrow_limit"]) * 10
    assert amount != pre_fix_buggy, "approval matches the pre-fix wrong-units pattern (borrow_limit * 10). REGRESSION."


def test_unsafe_infinite_approval_skipped_when_allowance_already_max() -> None:
    """If the existing allowance already covers MAX_UINT256, no redundant
    approve tx should be sent. Validates that ensureAllowance still short-
    circuits under the new opt-in path."""
    provider = _make_provider()
    wallet = SpyWallet(current_allowance=MAX_UINT256)

    args = dict(_BASE_ARGS, unsafe_infinite_approval=True)
    with patch.object(provider, "_facilitator_fetch", side_effect=_mock_facilitator_fetch):
        provider.grant_credit_delegation(wallet, args)

    assert _approve_txs(wallet, _BASE_ARGS["collateral_token"]) == [], (
        "approve tx should not be sent when allowance already sufficient"
    )


# ────────────────────────────────────────────────────────────────────────────
# collateral_approval=<raw> → exact bounded amount
# ────────────────────────────────────────────────────────────────────────────


def test_collateral_approval_grants_exact_bounded_amount() -> None:
    bounded = 123_456_789  # arbitrary scoped amount
    provider = _make_provider()
    wallet = SpyWallet(current_allowance=0)

    args = dict(_BASE_ARGS, collateral_approval=str(bounded))
    with patch.object(provider, "_facilitator_fetch", side_effect=_mock_facilitator_fetch):
        result = provider.grant_credit_delegation(wallet, args)

    assert "Error" not in result, f"unexpected error: {result}"

    approves = _approve_txs(wallet, _BASE_ARGS["collateral_token"])
    assert len(approves) == 1, f"expected exactly 1 approve tx, got {len(approves)}"

    spender, amount = _decode_approve(approves[0]["data"])
    assert spender.lower() == MATCHER_ADDRESS.lower()
    assert amount == bounded, (
        f"approval amount must be exactly {bounded}, got {amount}. Bounded approval must NOT be widened."
    )
    assert amount != MAX_UINT256, "bounded path leaked MAX_UINT256 — the bug we're fixing"


def test_collateral_approval_force_sets_down_when_current_exceeds_requested() -> None:
    """Migration scenario flagged in PR review (Copilot on agentkit-actions
    PR #17): a wallet that previously received the old MAX_UINT256 default
    re-runs grant_credit_delegation with collateral_approval=<raw>. The
    bounded path MUST issue an approve(requested) to actually reduce the
    allowance — otherwise the caller walks away with the old infinite
    allowance still active and a false sense of bounded exposure."""
    bounded = 1_000
    provider = _make_provider()
    wallet = SpyWallet(current_allowance=MAX_UINT256)  # legacy infinite allowance

    args = dict(_BASE_ARGS, collateral_approval=str(bounded))
    with patch.object(provider, "_facilitator_fetch", side_effect=_mock_facilitator_fetch):
        provider.grant_credit_delegation(wallet, args)

    approves = _approve_txs(wallet, _BASE_ARGS["collateral_token"])
    assert len(approves) == 1, f"expected exactly 1 approve tx (force-set DOWN), got {len(approves)}"
    spender, amount = _decode_approve(approves[0]["data"])
    assert spender.lower() == MATCHER_ADDRESS.lower()
    # Force-set down to the exact requested cap, not the existing MAX.
    assert amount == bounded, (
        f"bounded path must force-set down to {bounded}, got {amount}. "
        f"If amount == MAX_UINT256, the at-least-semantics bug has been reintroduced."
    )
    assert amount != MAX_UINT256, "bounded path silently retained MAX_UINT256 — REGRESSION"


def test_collateral_approval_skips_tx_only_when_current_exactly_equals_requested() -> None:
    """Gas-saving short-circuit on the bounded path: when current allowance
    is already exactly the requested amount, no tx is needed. The skip
    condition is equality, not >= — that's the whole point of the force-set
    fix above."""
    bounded = 1_000
    provider = _make_provider()
    wallet = SpyWallet(current_allowance=bounded)  # already at exact bound

    args = dict(_BASE_ARGS, collateral_approval=str(bounded))
    with patch.object(provider, "_facilitator_fetch", side_effect=_mock_facilitator_fetch):
        provider.grant_credit_delegation(wallet, args)

    assert _approve_txs(wallet, _BASE_ARGS["collateral_token"]) == [], (
        "approve tx should be skipped when current allowance already equals requested bound"
    )


# ────────────────────────────────────────────────────────────────────────────
# Both flags set → handler-level error, no side effects
# ────────────────────────────────────────────────────────────────────────────


def test_both_flags_set_returns_error_without_side_effects() -> None:
    """Mutually-exclusive choice is enforced in the handler (not the schema)
    so callers get an actionable error, but the action MUST refuse to do any
    on-chain or facilitator work — neither is reversible."""
    provider = _make_provider()
    wallet = SpyWallet(current_allowance=0)

    args = dict(
        _BASE_ARGS,
        collateral_approval="42",
        unsafe_infinite_approval=True,
    )

    facilitator_calls: list[str] = []

    def tracking_fetch(path: str, method: str = "GET", body: Any = None) -> dict[str, Any]:
        facilitator_calls.append(path)
        return _mock_facilitator_fetch(path, method, body)

    with patch.object(provider, "_facilitator_fetch", side_effect=tracking_fetch):
        result = provider.grant_credit_delegation(wallet, args)

    assert "pick one" in result.lower(), f"expected mutual-exclusion error message, got: {result}"
    assert wallet.sent == [], "no transactions should be sent when both approval flags are set"
    assert facilitator_calls == [], "no facilitator calls should be made when both approval flags are set"


# ────────────────────────────────────────────────────────────────────────────
# Schema acceptance: all four combinations are valid at the schema layer.
# Mutual exclusion is enforced in the handler so existing callers keep
# working at the framework boundary.
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"collateral_approval": "1000"},
        {"unsafe_infinite_approval": True},
        {"collateral_approval": "1000", "unsafe_infinite_approval": True},
    ],
    ids=["neither", "collateral_only", "unsafe_only", "both"],
)
def test_schema_accepts_all_approval_combinations(extra: dict[str, Any]) -> None:
    """Schema is permissive on approval fields by design — handler enforces
    the mutual-exclusion contract. Validates we did not accidentally tighten
    the schema and break callers who never set either field."""
    GrantCreditDelegationSchema(**dict(_BASE_ARGS, **extra))


# ────────────────────────────────────────────────────────────────────────────
# Schema rejects collateral_token outside the WETH/cbBTC whitelist.
# The bounded-approval handler relies on this restriction to skip the
# USDT-style approve(0)-first dance — if the whitelist isn't enforced, a
# caller could pass a token that reverts on direct approve(N) and end up
# with the facilitator delegation set on-chain but no working approval.
# ────────────────────────────────────────────────────────────────────────────


def test_schema_rejects_unknown_collateral_token() -> None:
    from pydantic import ValidationError

    bad_token = "0xdeadbeef00000000000000000000000000000001"
    with pytest.raises(ValidationError) as excinfo:
        GrantCreditDelegationSchema(**dict(_BASE_ARGS, collateral_token=bad_token))
    assert "WETH" in str(excinfo.value) or "cbBTC" in str(excinfo.value), (
        f"expected whitelist error mentioning the allowed tokens, got: {excinfo.value}"
    )


def test_schema_accepts_weth_and_cbbtc() -> None:
    weth = "0x4200000000000000000000000000000000000006"
    cbbtc = "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf"
    GrantCreditDelegationSchema(**dict(_BASE_ARGS, collateral_token=weth))
    GrantCreditDelegationSchema(**dict(_BASE_ARGS, collateral_token=cbbtc))


# ────────────────────────────────────────────────────────────────────────────
# Malformed `collateral_approval` values must be rejected BEFORE any
# facilitator pre-register or on-chain setOperator. Previously the parse
# happened in Step 3, so a bad value would only surface after the
# delegation was already set on-chain — leaving the caller to manually
# revoke. The fix moves parse + range check into the existing local
# validation block.
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_value, message_substring",
    [
        ("abc", "Invalid delegation input"),
        ("-5", "non-negative"),
        ("1.5", "Invalid delegation input"),
        # > uint256 max: 2**256, just past the limit.
        (str(1 << 256), "uint256"),
    ],
    ids=["non_numeric", "negative", "fractional", "overflow"],
)
def test_invalid_collateral_approval_fails_fast_without_side_effects(bad_value: str, message_substring: str) -> None:
    provider = _make_provider()
    wallet = SpyWallet(current_allowance=0)
    args = dict(_BASE_ARGS, collateral_approval=bad_value)

    facilitator_calls: list[str] = []

    def tracking_fetch(path: str, method: str = "GET", body: Any = None) -> dict[str, Any]:
        facilitator_calls.append(path)
        return _mock_facilitator_fetch(path, method, body)

    with patch.object(provider, "_facilitator_fetch", side_effect=tracking_fetch):
        result = provider.grant_credit_delegation(wallet, args)

    # Bad input must be caught before any side effects land.
    assert wallet.sent == [], (
        f"no transactions should be sent for invalid collateral_approval={bad_value!r}; got {wallet.sent}"
    )
    assert facilitator_calls == [], (
        f"no facilitator calls should be made for invalid collateral_approval={bad_value!r}; got {facilitator_calls}"
    )
    assert message_substring.lower() in result.lower(), (
        f"expected error mentioning {message_substring!r}, got: {result}"
    )
