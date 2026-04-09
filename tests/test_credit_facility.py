"""Tests for the credit-facility actions ported from TS agentkit.

Covers all 7 credit-facility actions now at parity with the TS port
(closed in commit 854fd92):

- check_credit_status, repay_credit       — reuse existing helpers
                                            (resolve_token_meta,
                                            _ensure_allowance) and matcher
                                            reads (getLoan, isHealthy,
                                            getAccruedInterest, etc.)
- request_credit, manual_match_credit,    — use the RPC event-log scan
  renew_credit_line                         path (_scan_available_lend_intents)
                                            and two/three-tx flows
- instant_borrow, repay_and_reborrow      — compose the above; also
                                            cover the on_behalf_of
                                            threading regression

Tests here monkey-patch _scan_available_lend_intents in a few places
to avoid needing a live RPC endpoint.
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
    loan.marketId = b"\x11" * 32
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

    def test_reverted_repay_returns_error_not_success(
        self, provider: FloeActionProvider
    ):
        """Regression for round 5: if wait_for_transaction_receipt returns
        a status=0 receipt (tx reverted), repay_credit must NOT claim
        success — downstream callers like repay_and_reborrow would
        otherwise proceed against a still-open loan."""
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", _build_active_loan())
        wallet.mock_read(BASE_MAINNET_MATCHER, "getAccruedInterest", (5 * 10**6, 86400))
        wallet.mock_read(
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "allowance",
            2**256 - 1,
        )
        wallet.mock_send("0xrepayreverted")

        # Override wait_for_transaction_receipt to simulate a reverted tx
        wallet.wait_for_transaction_receipt = lambda tx: {  # type: ignore[method-assign]
            "transactionHash": tx,
            "status": 0,  # ← reverted
        }

        result = provider.repay_credit(wallet, {"loan_id": "1"})
        assert "Credit Facility Repaid" not in result
        assert "reverted" in result.lower()
        assert "still open" in result.lower()

    def test_receipt_wait_failure_surfaces_as_error(
        self, provider: FloeActionProvider
    ):
        """Regression for round 5: if wait_for_transaction_receipt raises
        (RPC error, timeout, etc.), repay_credit must surface a clear
        'could not be confirmed' error — not silently report success."""
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", _build_active_loan())
        wallet.mock_read(BASE_MAINNET_MATCHER, "getAccruedInterest", (5 * 10**6, 86400))
        wallet.mock_read(
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "allowance",
            2**256 - 1,
        )
        wallet.mock_send("0xrepaynorpc")

        def raise_rpc_error(_tx: str) -> dict:
            raise RuntimeError("RPC timeout")

        wallet.wait_for_transaction_receipt = raise_rpc_error  # type: ignore[method-assign]

        result = provider.repay_credit(wallet, {"loan_id": "1"})
        assert "Credit Facility Repaid" not in result
        assert "could not be confirmed" in result
        assert "0xrepaynorpc" in result

    def test_full_repay_includes_min_interest_penalty(
        self, provider: FloeActionProvider
    ):
        """Regression for round 5: full-repay cap must include the
        early-repayment penalty when the loan has nonzero minInterestBps
        and is repaid before maturity. Without this, repayLoan() reverts
        because the contract requires full_term_interest * minInterestBps /
        BASIS_POINTS and the slippage cushion is too small.

        Asserts via the Max Total Repayment line in the response: a loan
        with 100% minInterestBps (= charge full-term interest even on
        early repay) must show a max >= principal + full_term_interest.
        """
        wallet = MockWalletProvider()
        loan = _build_active_loan()
        loan.minInterestBps = 10000  # 100% of full-term interest
        loan.interestRateBps = 500   # 5% APR
        loan.duration = 365 * 24 * 60 * 60  # 1 year → full_term = principal * 5%
        loan.principal = 1000 * 10**6  # 1000 USDC
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", loan)
        # Only 1 USDC actually accrued (very early in the loan)
        wallet.mock_read(BASE_MAINNET_MATCHER, "getAccruedInterest", (1 * 10**6, 3600))
        wallet.mock_read(
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "allowance",
            2**256 - 1,
        )
        wallet.mock_send("0xrepayminint")

        result = provider.repay_credit(wallet, {"loan_id": "1"})
        assert "Credit Facility Repaid" in result
        # Without the minInterestBps fix, max total repayment would be
        # ~1001 USDC (principal + 1 accrued + 5% slippage = ~1051).
        # With the fix, it must include the ~50 USDC penalty (full term
        # interest floor) → principal + 50 + 5% slippage ≈ 1102 USDC.
        # The response formats Max Total Repayment via format_token_amount.
        assert "Early Repay Penalty" in result
        # Verify the penalty line appears: full_term (50 USDC) - accrued (1 USDC) ≈ 49 USDC
        # format_token_amount with 2 decimals displays "49.00 USDC"
        assert "49.00 USDC" in result

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

    # ── Preflight compatibility regressions ───────────────────────────────
    # Verify that manual_match_credit rejects each class of incompatible
    # offer BEFORE sending TX1 (registerBorrowIntent). Without these
    # preflight checks, TX1 would land on-chain and burn gas before TX2
    # (matchLoanIntents) reverts with a cryptic error.

    def _base_args(self) -> dict:
        return {
            "lend_intent_hash": _LEND_HASH,
            "borrow_amount": str(1000 * 10**6),
            "collateral_amount": str(10**18),
            "max_interest_rate_bps": "1000",
            "min_ltv_bps": "7000",
            "duration": "604800",
            "market_id": _MARKET_ID,
        }

    def _preflight_wallet(self, intent) -> MockWalletProvider:
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getMarket", _build_market())
        wallet.mock_read(BASE_MAINNET_MATCHER, "getOnChainLendIntent", intent)
        # Should NEVER reach send_transaction on these paths — preflight
        # rejects before TX1. If any of these tests fails via "no mock send
        # response", it means the preflight let the request through.
        return wallet

    def test_preflight_rejects_wrong_market(self, provider: FloeActionProvider):
        intent = _build_lend_intent()
        intent.marketId = b"\x99" * 32  # different market than _MARKET_ID
        result = provider.manual_match_credit(self._preflight_wallet(intent), self._base_args())
        assert "belongs to market" in result

    def test_preflight_rejects_future_valid_from(self, provider: FloeActionProvider):
        import time as _t
        intent = _build_lend_intent()
        intent.validFromTimestamp = int(_t.time()) + 86400  # one day from now
        result = provider.manual_match_credit(self._preflight_wallet(intent), self._base_args())
        assert "not yet valid" in result

    def test_preflight_rejects_rate_floor_above_ceiling(self, provider: FloeActionProvider):
        # Lender demands 20% floor; borrower caps at 10% → incompatible
        intent = _build_lend_intent(rate_bps=2000)
        result = provider.manual_match_credit(self._preflight_wallet(intent), self._base_args())
        assert "minimum rate" in result

    def test_preflight_rejects_ltv_exceeds_max(self, provider: FloeActionProvider):
        intent = _build_lend_intent()
        intent.maxLtvBps = 6000  # lender caps at 60%; borrower wants 70%
        result = provider.manual_match_credit(self._preflight_wallet(intent), self._base_args())
        assert "max LTV" in result

    def test_preflight_rejects_duration_outside_window(self, provider: FloeActionProvider):
        intent = _build_lend_intent(min_dur=30 * 86400, max_dur=90 * 86400)
        args = self._base_args()
        args["duration"] = str(7 * 86400)  # 7 days, below lender's 30-day minimum
        result = provider.manual_match_credit(self._preflight_wallet(intent), args)
        assert "duration" in result

    def test_preflight_rejects_disallowed_partial_fill(self, provider: FloeActionProvider):
        """Regression for round 5: a lend intent with allowPartialFill=false
        can only be matched by a borrow that fills it EXACTLY. Since
        _build_borrow_struct always posts exact-fill (partial_fill=false),
        any mismatch between borrow_amount and remaining must be rejected
        before TX1."""
        intent = _build_lend_intent(amount=5000 * 10**6)
        intent.allowPartialFill = False
        # remaining = 5000 USDC but caller requests 1000 USDC — incompatible
        result = provider.manual_match_credit(self._preflight_wallet(intent), self._base_args())
        assert "partial fills" in result
        assert "match exactly" in result

    def test_preflight_rejects_below_min_fill(self, provider: FloeActionProvider):
        """Regression for round 5: borrow_amount below the lender's
        minFillAmount must be rejected before TX1."""
        intent = _build_lend_intent(amount=5000 * 10**6)
        intent.minFillAmount = 2000 * 10**6  # lender requires at least 2000 USDC per fill
        # Caller requests 1000 USDC — below min fill
        result = provider.manual_match_credit(self._preflight_wallet(intent), self._base_args())
        assert "minimum fill" in result

    # ── Receipt status regressions (round 8) ─────────────────────────────
    # manual_match_credit must reject status=0 (reverted) and exception
    # (unconfirmable) receipts from BOTH TX1 and TX2, exactly like
    # repay_credit does. Missing this check meant a reverted
    # registerBorrowIntent or matchLoanIntents would surface as
    # "## Credit Facility Opened" and the callers that delegate here
    # (instant_borrow, renew_credit_line) would inherit the false success.

    def _happy_path_wallet(self) -> MockWalletProvider:
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
        return wallet

    def test_reverted_tx1_returns_error_not_success(self, provider: FloeActionProvider):
        wallet = self._happy_path_wallet()
        # TX1 receipt reverts
        wallet.wait_for_transaction_receipt = lambda tx: {  # type: ignore[method-assign]
            "transactionHash": tx,
            "status": 0,
        }
        result = provider.manual_match_credit(wallet, self._base_args())
        assert "Credit Facility Opened" not in result
        assert "Register borrow intent reverted" in result

    def test_reverted_tx2_returns_error_not_success(self, provider: FloeActionProvider):
        wallet = self._happy_path_wallet()
        # TX1 succeeds, TX2 reverts
        call_count = {"n": 0}

        def mock_wait(tx: str) -> dict:
            call_count["n"] += 1
            return {"transactionHash": tx, "status": 1 if call_count["n"] == 1 else 0}

        wallet.wait_for_transaction_receipt = mock_wait  # type: ignore[method-assign]
        result = provider.manual_match_credit(wallet, self._base_args())
        assert "Credit Facility Opened" not in result
        assert "Match loan intents reverted" in result
        # TX1 hash must be mentioned so the operator can revoke the stray intent
        assert "0xregister" in result

    def test_tx1_receipt_wait_failure_surfaces_as_error(self, provider: FloeActionProvider):
        wallet = self._happy_path_wallet()

        def raise_rpc(_tx: str) -> dict:
            raise RuntimeError("RPC timeout on TX1")

        wallet.wait_for_transaction_receipt = raise_rpc  # type: ignore[method-assign]
        result = provider.manual_match_credit(wallet, self._base_args())
        assert "Credit Facility Opened" not in result
        assert "could not be confirmed" in result
        assert "0xregister" in result


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

    def test_reverted_repay_does_not_proceed_to_reborrow(
        self, provider: FloeActionProvider
    ):
        """Regression for round 8: repay_and_reborrow must NOT continue
        into reborrow when repay_credit returns a reverted-receipt plain
        string. Previously the blacklist check 'startswith(Error)'
        treated that as success because the reverted message starts
        with 'Repay transaction' not 'Error'."""
        wallet = MockWalletProvider()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", _build_active_loan())
        wallet.mock_read(
            BASE_MAINNET_MATCHER, "getAccruedInterest", (5 * 10**6, 86400)
        )
        wallet.mock_read(
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "allowance",
            2**256 - 1,
        )
        wallet.mock_send("0xrepayreverted")
        # Simulate reverted repay receipt — repay_credit will return a
        # "Repay transaction ... reverted on-chain" plain string that
        # does NOT start with "Error".
        wallet.wait_for_transaction_receipt = lambda tx: {  # type: ignore[method-assign]
            "transactionHash": tx,
            "status": 0,
        }

        result = provider.repay_and_reborrow(wallet, {"loan_id": "1"})
        # Must bubble up the repay failure, NOT a "Credit Line Renewed"
        # message, and definitely not try to open a new facility.
        assert "reverted" in result.lower()
        assert "Credit Line Renewed" not in result
        assert "Credit Facility Opened" not in result


# ── on_behalf_of threading (regression for Copilot #2/#3/#4) ───────────────
#
# These tests lock down that the credit-facility actions forward the
# caller-supplied `on_behalf_of` into the BorrowIntent struct instead of
# hardcoding it to the caller's own address. The schemas advertise the
# field; silently ignoring it would be a misleading API.
#
# Strategy: monkey-patch `_build_borrow_struct` to capture the kwargs it
# receives, then assert the captured value matches the expected recipient.
# We short-circuit the rest of the flow by returning a deterministic tuple
# so the txs can still be encoded and sent without errors.


_RECIPIENT_ALICE = "0x4200000000000000000000000000000000000006"  # WETH — known checksum-safe
# Use known checksum-valid addresses — web3.py encode_abi rejects
# non-checksummed mixed-case hex at serialization time.
_DUMMY_BORROW_TUPLE = (
    "0x4200000000000000000000000000000000000006",  # borrower (WETH addr, all-digit so checksum-safe)
    _RECIPIENT_ALICE,                                # on_behalf_of (all-uppercase is checksum-safe)
    1000 * 10**6,                                    # borrow_amount
    10**18,                                          # collateral_amount
    1000 * 10**6,                                    # min_fill_amount
    1000,                                            # max_rate_bps
    7000,                                            # min_ltv_bps
    604800,                                          # min_duration
    604800,                                          # max_duration
    False,                                           # allow_partial_fill
    0,                                               # valid_from_timestamp
    50,                                              # matcher_commission_bps
    1_999_999_999,                                   # expiry
    b"\x11" * 32,                                    # market_id
    b"\x22" * 32,                                    # salt
    [],                                              # conditions
    [],                                              # pre_hooks
    [],                                              # post_hooks
)


class TestOnBehalfOfPropagation:
    """Regression: on_behalf_of must flow into _build_borrow_struct."""

    def _spy(self, provider: FloeActionProvider, monkeypatch) -> dict:
        """Replace _build_borrow_struct with a capturing stub."""
        captured: dict = {}

        def stub(**kwargs):
            captured.update(kwargs)
            # Return a valid-shape tuple so the rest of the flow
            # (encode_abi, send_transaction) doesn't error.
            return _DUMMY_BORROW_TUPLE

        monkeypatch.setattr(provider, "_build_borrow_struct", stub)
        return captured

    def test_manual_match_credit_threads_on_behalf_of(
        self, provider: FloeActionProvider, monkeypatch
    ):
        captured = self._spy(provider, monkeypatch)
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
                "on_behalf_of": _RECIPIENT_ALICE,
            },
        )
        assert "Credit Facility Opened" in result
        assert captured["on_behalf_of"] == _RECIPIENT_ALICE
        # Borrower is still the caller (wallet owner), not the recipient
        assert captured["borrower"] == wallet.get_address()

    def test_manual_match_credit_defaults_to_caller_when_omitted(
        self, provider: FloeActionProvider, monkeypatch
    ):
        captured = self._spy(provider, monkeypatch)
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

        provider.manual_match_credit(
            wallet,
            {
                "lend_intent_hash": _LEND_HASH,
                "borrow_amount": str(1000 * 10**6),
                "collateral_amount": str(10**18),
                "max_interest_rate_bps": "1000",
                "min_ltv_bps": "7000",
                "duration": "604800",
                "market_id": _MARKET_ID,
                # on_behalf_of omitted
            },
        )
        # Default: on_behalf_of falls back to the caller's address
        assert captured["on_behalf_of"] == wallet.get_address()

    def test_instant_borrow_threads_on_behalf_of(
        self, provider: FloeActionProvider, monkeypatch
    ):
        captured = self._spy(provider, monkeypatch)
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
            {"hash": "0x" + "aa" * 32, "intent": _build_lend_intent(rate_bps=300)},
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
                "on_behalf_of": _RECIPIENT_ALICE,
            },
        )
        assert "Credit Facility Opened" in result
        assert captured["on_behalf_of"] == _RECIPIENT_ALICE

    def test_repay_and_reborrow_threads_on_behalf_of(
        self, provider: FloeActionProvider, monkeypatch
    ):
        captured = self._spy(provider, monkeypatch)
        wallet = MockWalletProvider()

        # Step 1: old loan state for repay_credit
        old_loan = _build_active_loan()
        wallet.mock_read(BASE_MAINNET_MATCHER, "getLoan", old_loan)
        wallet.mock_read(
            BASE_MAINNET_MATCHER, "getAccruedInterest", (5 * 10**6, 86400)
        )
        wallet.mock_read(
            "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC allowance
            "allowance",
            2**256 - 1,
        )
        # Step 2: market + lend intent for the new borrow
        wallet.mock_read(BASE_MAINNET_MATCHER, "getMarket", _build_market())
        wallet.mock_read(
            BASE_MAINNET_MATCHER, "getOnChainLendIntent", _build_lend_intent(rate_bps=300)
        )
        wallet.mock_read(
            "0x4200000000000000000000000000000000000006",  # WETH allowance
            "allowance",
            2**256 - 1,
        )
        # Three txs: repay, register, match
        wallet.mock_send("0xrepay")
        wallet.mock_send("0xregister")
        wallet.mock_send("0xmatch")

        offers = [
            {"hash": "0x" + "aa" * 32, "intent": _build_lend_intent(rate_bps=300)},
        ]
        monkeypatch.setattr(
            provider, "_scan_available_lend_intents", lambda w, m: offers
        )

        result = provider.repay_and_reborrow(
            wallet,
            {
                "loan_id": "1",
                "on_behalf_of": _RECIPIENT_ALICE,
            },
        )
        assert "Credit Line Renewed" in result
        assert captured["on_behalf_of"] == _RECIPIENT_ALICE
