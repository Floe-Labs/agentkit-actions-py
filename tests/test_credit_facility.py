"""Tests for the credit-facility actions ported from TS agentkit (Phase A).

Phase A scope: check_credit_status, repay_credit. Both reuse existing
helpers (resolve_token_meta, _ensure_allowance) and existing matcher
read paths (getLoan, getCurrentLtvBps, isHealthy, getAccruedInterest).

Phase A.2 (request_credit, manual_match_credit, renew_credit_line) and
Phase B (instant_borrow, repay_and_reborrow) are tracked separately —
they require new infrastructure (RPC client + event log scanning, or
the floe-credit-sdk Python adapter respectively).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from floe_agentkit_actions.action_provider import FloeActionProvider
from floe_agentkit_actions.constants import BASE_MAINNET_MATCHER

from tests.conftest import MockWalletProvider


@pytest.fixture
def provider() -> FloeActionProvider:
    return FloeActionProvider()


# ── check_credit_status ────────────────────────────────────────────────────


def _build_active_loan(
    *,
    start_offset_seconds: int = -3600,  # 1 hour ago
    duration: int = 2592000,            # 30 days
    principal: int = 1000 * 10**6,
    repaid: bool = False,
) -> MagicMock:
    """Build a fresh active loan with a current timestamp.

    The shared `mock_wallet_with_loan` fixture in conftest.py uses a
    hard-coded startTime from 2023, which makes loans look permanently
    overdue when the credit-status formatter checks against `time.time()`.
    Tests that need a non-overdue loan must build their own.
    """
    import time as _time

    loan = MagicMock()
    loan.repaid = repaid
    loan.loanToken = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC
    loan.collateralToken = "0x4200000000000000000000000000000000000006"  # WETH
    loan.principal = principal
    loan.interestRateBps = 500
    loan.ltvBps = 7000
    loan.liquidationLtvBps = 8500
    loan.marketFeeBps = 100
    loan.matcherCommissionBps = 50
    loan.startTime = int(_time.time()) + start_offset_seconds
    loan.duration = duration
    loan.collateralAmount = 10**18
    loan.gracePeriod = 86400
    loan.minInterestBps = 0
    return loan


class TestCheckCreditStatus:
    def test_active_healthy_loan(self, provider: FloeActionProvider):
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", _build_active_loan())
        wallet.mock_read(BASE_MAINNET_MATCHER, "getCurrentLtvBps", 6500)
        wallet.mock_read(BASE_MAINNET_MATCHER, "isHealthy", True)
        wallet.mock_read(BASE_MAINNET_MATCHER, "getAccruedInterest", (5 * 10**6, 86400))

        result = provider.check_credit_status(wallet, {"loan_id": "1"})

        assert "Credit Facility Status -- Loan #1" in result
        assert "Healthy" in result
        # currentLtv 6500, liquidationLtv 8500 → distance 2000 bps > 500 → no warning
        assert "Liquidatable" not in result
        assert "Principal" in result
        assert "Accrued Interest" in result
        # Healthy + buffer 2000 bps + not overdue + plenty of time = no critical notes
        assert "CRITICAL" not in result
        assert "OVERDUE" not in result

    def test_repaid_loan_returns_terminal_message(
        self,
        provider: FloeActionProvider,
    ):
        wallet = MockWalletProvider()
        loan = MagicMock()
        loan.repaid = True
        loan.loanToken = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        loan.collateralToken = "0x4200000000000000000000000000000000000006"
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", loan)
        wallet.mock_read(BASE_MAINNET_MATCHER, "getCurrentLtvBps", 0)
        wallet.mock_read(BASE_MAINNET_MATCHER, "isHealthy", True)
        wallet.mock_read(BASE_MAINNET_MATCHER, "getAccruedInterest", (0, 0))

        result = provider.check_credit_status(wallet, {"loan_id": "1"})

        assert "Fully repaid" in result
        assert "No active credit facility" in result
        # Confirm we did NOT render the full status block for a repaid loan
        assert "Credit Facility Status" not in result

    def test_unhealthy_loan_triggers_critical_warning(
        self,
        provider: FloeActionProvider,
    ):
        wallet = MockWalletProvider()
        loan = MagicMock()
        loan.repaid = False
        loan.loanToken = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        loan.collateralToken = "0x4200000000000000000000000000000000000006"
        loan.principal = 1000 * 10**6
        loan.interestRateBps = 500
        loan.ltvBps = 7000
        loan.liquidationLtvBps = 8500
        loan.marketFeeBps = 100
        loan.matcherCommissionBps = 50
        loan.startTime = 1700000000
        loan.duration = 2592000  # 30 days
        loan.collateralAmount = 10**18
        loan.gracePeriod = 86400
        loan.minInterestBps = 0

        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", loan)
        # Unhealthy: current LTV above liquidation threshold
        wallet.mock_read(BASE_MAINNET_MATCHER, "getCurrentLtvBps", 9000)
        wallet.mock_read(BASE_MAINNET_MATCHER, "isHealthy", False)
        wallet.mock_read(BASE_MAINNET_MATCHER, "getAccruedInterest", (5 * 10**6, 86400))

        result = provider.check_credit_status(wallet, {"loan_id": "1"})

        assert "Credit Facility Status" in result
        assert "NO -- Liquidatable!" in result
        assert "**CRITICAL**" in result
        assert "Repay immediately or add collateral" in result

    def test_invalid_loan_id_returns_error(self, provider: FloeActionProvider):
        wallet = MockWalletProvider()
        # No mocks set → read_contract will raise
        result = provider.check_credit_status(wallet, {"loan_id": "999"})
        assert "Error checking credit status" in result


# ── repay_credit ───────────────────────────────────────────────────────────


class TestRepayCredit:
    def test_full_repay_uses_principal_as_amount(self, provider: FloeActionProvider):
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", _build_active_loan())
        wallet.mock_read(BASE_MAINNET_MATCHER, "getAccruedInterest", (5 * 10**6, 86400))
        # Pre-mock the allowance so _ensure_allowance returns None (no approve tx)
        wallet.mock_read(
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
            "allowance",
            2**256 - 1,
        )
        wallet.mock_send("0xrepay" + "00" * 30)

        result = provider.repay_credit(wallet, {"loan_id": "1"})

        assert "Credit Facility Repaid" in result
        assert "1,000.00 USDC" in result  # principal = 1000 USDC, format_token_amount uses 2 decimals
        assert "Loan ID" in result
        assert "Allowance sufficient" in result

    def test_already_repaid_loan_short_circuits(self, provider: FloeActionProvider):
        wallet = MockWalletProvider()
        loan = MagicMock()
        loan.repaid = True
        loan.principal = 0
        loan.loanToken = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", loan)
        wallet.mock_read(BASE_MAINNET_MATCHER, "getAccruedInterest", (0, 0))

        result = provider.repay_credit(wallet, {"loan_id": "1"})

        assert "already repaid" in result
        # Should NOT have submitted any tx
        assert "Transaction" not in result

    def test_zero_principal_short_circuits(self, provider: FloeActionProvider):
        wallet = MockWalletProvider()
        loan = MagicMock()
        loan.repaid = False
        loan.principal = 0
        loan.loanToken = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", loan)
        wallet.mock_read(BASE_MAINNET_MATCHER, "getAccruedInterest", (0, 0))

        result = provider.repay_credit(wallet, {"loan_id": "1"})

        assert "zero principal" in result.lower() or "already settled" in result.lower()

    def test_slippage_default_500_bps(self, provider: FloeActionProvider):
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", _build_active_loan())
        wallet.mock_read(BASE_MAINNET_MATCHER, "getAccruedInterest", (5 * 10**6, 86400))
        wallet.mock_read(
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "allowance",
            2**256 - 1,
        )
        wallet.mock_send("0xrepay")

        result = provider.repay_credit(wallet, {"loan_id": "1"})
        # Default slippage = 500 bps = 5%
        assert "5.00%" in result

    def test_custom_slippage_passed_through(self, provider: FloeActionProvider):
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", _build_active_loan())
        wallet.mock_read(BASE_MAINNET_MATCHER, "getAccruedInterest", (5 * 10**6, 86400))
        wallet.mock_read(
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "allowance",
            2**256 - 1,
        )
        wallet.mock_send("0xrepay")

        result = provider.repay_credit(wallet, {"loan_id": "1", "slippage_bps": "1000"})
        # 1000 bps = 10%
        assert "10.00%" in result


# ── Phase A.2 / B: request_credit, manual_match_credit, renew_credit_line,
#                  instant_borrow, repay_and_reborrow ─────────────────────


def _build_market() -> MagicMock:
    m = MagicMock()
    m.loanToken = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"  # USDC
    m.collateralToken = "0x4200000000000000000000000000000000000006"  # WETH
    return m


def _build_lend_intent(
    *,
    amount: int = 5000 * 10**6,
    filled: int = 0,
    rate_bps: int = 500,
    min_dur: int = 86400,
    max_dur: int = 30 * 86400,
) -> MagicMock:
    import time as _t
    i = MagicMock()
    i.lender = "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    i.onBehalfOf = "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    i.amount = amount
    i.minFillAmount = 1
    i.filledAmount = filled
    i.minInterestRateBps = rate_bps
    i.maxLtvBps = 8000
    i.minDuration = min_dur
    i.maxDuration = max_dur
    i.allowPartialFill = True
    i.validFromTimestamp = 0
    i.expiry = int(_t.time()) + 3600
    i.marketId = b"\x11" * 32
    i.salt = b"\x22" * 32
    i.gracePeriod = 0
    i.minInterestBps = 0
    return i


_MARKET_ID = "0x" + "11" * 32
_LEND_HASH = "0x" + "22" * 32


class TestRequestCredit:
    def test_no_offers_returns_friendly_message(
        self, provider: FloeActionProvider, monkeypatch
    ):
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getMarket", _build_market())
        monkeypatch.setattr(provider, "_scan_available_lend_intents", lambda w, m: [])
        result = provider.request_credit(wallet, {"market_id": _MARKET_ID})
        assert "No Credit Offers Available" in result

    def test_lists_offers_sorted_by_remaining(
        self, provider: FloeActionProvider, monkeypatch
    ):
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getMarket", _build_market())
        offers = [
            {"hash": "0x" + "aa" * 32, "intent": _build_lend_intent(amount=1000 * 10**6)},
            {"hash": "0x" + "bb" * 32, "intent": _build_lend_intent(amount=5000 * 10**6)},
        ]
        monkeypatch.setattr(provider, "_scan_available_lend_intents", lambda w, m: offers)
        result = provider.request_credit(wallet, {"market_id": _MARKET_ID})
        assert "Available Credit Offers" in result
        assert "Found 2 offer(s)" in result
        # The 5000 USDC offer should be listed before the 1000 USDC offer
        assert result.index("0xbbbbbbbb") < result.index("0xaaaaaaaa")

    def test_filter_by_max_rate(self, provider: FloeActionProvider, monkeypatch):
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getMarket", _build_market())
        offers = [
            {"hash": "0x" + "aa" * 32, "intent": _build_lend_intent(rate_bps=300)},
            {"hash": "0x" + "bb" * 32, "intent": _build_lend_intent(rate_bps=900)},
        ]
        monkeypatch.setattr(provider, "_scan_available_lend_intents", lambda w, m: offers)
        result = provider.request_credit(
            wallet, {"market_id": _MARKET_ID, "max_rate_bps": "500"}
        )
        assert "Found 1 offer(s)" in result
        assert "0xaaaaaaaa" in result
        assert "0xbbbbbbbb" not in result


class TestManualMatchCredit:
    def test_happy_path_returns_credit_facility_opened(
        self, provider: FloeActionProvider
    ):
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getMarket", _build_market())
        wallet.mock_read(
            BASE_MAINNET_MATCHER, "getOnChainLendIntent", _build_lend_intent()
        )
        wallet.mock_read(
            "0x4200000000000000000000000000000000000006", "allowance", 2**256 - 1
        )
        wallet.mock_send("0xregister")
        wallet.mock_send("0xmatch")
        result = provider.manual_match_credit(
            wallet,
            {
                "lend_intent_hash": _LEND_HASH,
                "borrow_amount": str(1000 * 10**6),
                "collateral_amount": str(10**18),
                "max_interest_rate_bps": "1000",
                "min_ltv_bps": "7000",
                "duration": "604800",
                "market_id": _MARKET_ID,
            },
        )
        assert "Credit Facility Opened" in result
        assert "0xregister" in result
        assert "0xmatch" in result

    def test_revoked_intent_returns_error(self, provider: FloeActionProvider):
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getMarket", _build_market())
        revoked = _build_lend_intent()
        revoked.lender = "0x0000000000000000000000000000000000000000"
        wallet.mock_read(BASE_MAINNET_MATCHER, "getOnChainLendIntent", revoked)
        result = provider.manual_match_credit(
            wallet,
            {
                "lend_intent_hash": _LEND_HASH,
                "borrow_amount": "1000",
                "collateral_amount": "1",
                "max_interest_rate_bps": "1000",
                "min_ltv_bps": "7000",
                "duration": "604800",
                "market_id": _MARKET_ID,
            },
        )
        assert "not found on-chain" in result

    def test_insufficient_remaining_returns_error(self, provider: FloeActionProvider):
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getMarket", _build_market())
        intent = _build_lend_intent(amount=100 * 10**6)
        wallet.mock_read(BASE_MAINNET_MATCHER, "getOnChainLendIntent", intent)
        result = provider.manual_match_credit(
            wallet,
            {
                "lend_intent_hash": _LEND_HASH,
                "borrow_amount": str(1000 * 10**6),
                "collateral_amount": str(10**18),
                "max_interest_rate_bps": "1000",
                "min_ltv_bps": "7000",
                "duration": "604800",
                "market_id": _MARKET_ID,
            },
        )
        assert "remaining" in result


class TestInstantBorrow:
    def test_picks_lowest_rate_compatible_offer(
        self, provider: FloeActionProvider, monkeypatch
    ):
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getMarket", _build_market())
        wallet.mock_read(
            BASE_MAINNET_MATCHER, "getOnChainLendIntent", _build_lend_intent(rate_bps=300)
        )
        wallet.mock_read(
            "0x4200000000000000000000000000000000000006", "allowance", 2**256 - 1
        )
        wallet.mock_send("0xregister")
        wallet.mock_send("0xmatch")
        offers = [
            {"hash": "0x" + "aa" * 32, "intent": _build_lend_intent(rate_bps=800)},
            {"hash": "0x" + "bb" * 32, "intent": _build_lend_intent(rate_bps=300)},
        ]
        monkeypatch.setattr(
            provider, "_scan_available_lend_intents", lambda w, m: offers
        )
        result = provider.instant_borrow(
            wallet,
            {
                "market_id": _MARKET_ID,
                "borrow_amount": str(1000 * 10**6),
                "collateral_amount": str(10**18),
                "max_interest_rate_bps": "1000",
                "duration": "604800",
            },
        )
        assert "Credit Facility Opened" in result

    def test_no_compatible_returns_friendly_message(
        self, provider: FloeActionProvider, monkeypatch
    ):
        wallet = MockWalletProvider()
        offers = [
            {"hash": "0x" + "aa" * 32, "intent": _build_lend_intent(rate_bps=2000)},
        ]
        monkeypatch.setattr(
            provider, "_scan_available_lend_intents", lambda w, m: offers
        )
        result = provider.instant_borrow(
            wallet,
            {
                "market_id": _MARKET_ID,
                "borrow_amount": str(1000 * 10**6),
                "collateral_amount": str(10**18),
                "max_interest_rate_bps": "1000",
                "duration": "604800",
            },
        )
        assert "No matching liquidity" in result


class TestRenewCreditLine:
    def test_three_tx_happy_path(self, provider: FloeActionProvider):
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", _build_active_loan())
        wallet.mock_read(BASE_MAINNET_MATCHER, "getAccruedInterest", (5 * 10**6, 86400))
        wallet.mock_read(BASE_MAINNET_MATCHER, "getMarket", _build_market())
        wallet.mock_read(
            BASE_MAINNET_MATCHER, "getOnChainLendIntent", _build_lend_intent()
        )
        wallet.mock_read(
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "allowance", 2**256 - 1
        )
        wallet.mock_read(
            "0x4200000000000000000000000000000000000006", "allowance", 2**256 - 1
        )
        wallet.mock_send("0xrepay")
        wallet.mock_send("0xregister")
        wallet.mock_send("0xmatch")
        result = provider.renew_credit_line(
            wallet,
            {
                "loan_id": "1",
                "lend_intent_hash": _LEND_HASH,
                "borrow_amount": str(1000 * 10**6),
                "collateral_amount": str(10**18),
                "max_interest_rate_bps": "1000",
                "min_ltv_bps": "7000",
                "duration": "604800",
                "market_id": _MARKET_ID,
            },
        )
        assert "Credit Line Renewed" in result
        assert "0xrepay" in result


class TestRepayAndReborrow:
    def test_already_repaid_short_circuits(self, provider: FloeActionProvider):
        wallet = MockWalletProvider()
        loan = MagicMock()
        loan.repaid = True
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", loan)
        result = provider.repay_and_reborrow(wallet, {"loan_id": "1"})
        assert "already repaid" in result
