"""Tests for Pydantic schema validation."""

import pytest
from pydantic import ValidationError

from floe_agentkit_actions.schemas import (
    AddCollateralSchema,
    CheckFlashArbReadinessSchema,
    CheckLoanHealthSchema,
    DeployFlashArbReceiverSchema,
    EstimateFlashArbProfitSchema,
    FlashArbSchema,
    FlashLoanSchema,
    GetAccruedInterestSchema,
    GetFlashArbBalanceSchema,
    GetFlashLoanFeeSchema,
    GetIntentBookSchema,
    GetLiquidationQuoteSchema,
    GetLoanSchema,
    GetMarketsSchema,
    GetMyLoansSchema,
    GetPriceSchema,
    LiquidateLoanSchema,
    MatchIntentsSchema,
    PostBorrowIntentSchema,
    PostLendIntentSchema,
    RepayLoanSchema,
    VerifyFlashArbReceiverSchema,
    WithdrawCollateralSchema,
)

VALID_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
VALID_BYTES32 = "0x" + "ab" * 32


class TestReadSchemas:
    def test_get_markets_empty(self):
        schema = GetMarketsSchema()
        assert schema.market_ids is None

    def test_get_markets_with_ids(self):
        schema = GetMarketsSchema(market_ids=[VALID_BYTES32])
        assert len(schema.market_ids) == 1

    def test_get_markets_invalid_bytes32(self):
        with pytest.raises(ValidationError):
            GetMarketsSchema(market_ids=["0xinvalid"])

    def test_get_loan(self):
        schema = GetLoanSchema(loan_id="42")
        assert schema.loan_id == "42"

    def test_get_my_loans(self):
        GetMyLoansSchema()  # No fields

    def test_check_loan_health(self):
        schema = CheckLoanHealthSchema(loan_id="1")
        assert schema.loan_id == "1"

    def test_get_price(self):
        schema = GetPriceSchema(collateral_token=VALID_ADDRESS, loan_token=VALID_ADDRESS)
        assert schema.collateral_token == VALID_ADDRESS

    def test_get_price_invalid_address(self):
        with pytest.raises(ValidationError):
            GetPriceSchema(collateral_token="invalid", loan_token=VALID_ADDRESS)

    def test_get_accrued_interest(self):
        schema = GetAccruedInterestSchema(loan_id="5")
        assert schema.loan_id == "5"

    def test_get_liquidation_quote(self):
        schema = GetLiquidationQuoteSchema(loan_id="1", repay_amount="1000000")
        assert schema.repay_amount == "1000000"

    def test_get_intent_book_lend(self):
        schema = GetIntentBookSchema(intent_hash=VALID_BYTES32, intent_type="lend")
        assert schema.intent_type == "lend"

    def test_get_intent_book_invalid_type(self):
        with pytest.raises(ValidationError):
            GetIntentBookSchema(intent_hash=VALID_BYTES32, intent_type="invalid")


class TestWriteSchemas:
    def test_post_lend_intent(self):
        schema = PostLendIntentSchema(
            amount="1000000000",
            min_fill_amount="1000000000",
            min_interest_rate_bps="500",
            max_ltv_bps="8000",
            min_duration="86400",
            max_duration="2592000",
            market_id=VALID_BYTES32,
        )
        assert schema.allow_partial_fill is True  # default
        assert schema.expiry_seconds == "86400"  # default
        assert schema.grace_period == "86400"  # default
        assert schema.min_interest_bps == "0"  # default

    def test_post_borrow_intent(self):
        schema = PostBorrowIntentSchema(
            borrow_amount="1000000",
            collateral_amount="500000000000000000",
            min_fill_amount="1000000",
            max_interest_rate_bps="1000",
            min_ltv_bps="5000",
            min_duration="86400",
            max_duration="2592000",
            market_id=VALID_BYTES32,
        )
        assert schema.allow_partial_fill is False  # default
        assert schema.matcher_commission_bps == "50"  # default

    def test_match_intents(self):
        schema = MatchIntentsSchema(
            lend_intent_hash=VALID_BYTES32,
            borrow_intent_hash=VALID_BYTES32,
            market_id=VALID_BYTES32,
        )
        assert schema.lend_intent_hash == VALID_BYTES32

    def test_repay_loan(self):
        schema = RepayLoanSchema(loan_id="1", repay_amount="1000000")
        assert schema.slippage_bps == "500"  # default

    def test_add_collateral(self):
        schema = AddCollateralSchema(loan_id="1", amount="500000000000000000")
        assert schema.amount == "500000000000000000"

    def test_withdraw_collateral(self):
        schema = WithdrawCollateralSchema(loan_id="1", amount="100000000000000000")
        assert schema.loan_id == "1"

    def test_liquidate_loan(self):
        schema = LiquidateLoanSchema(loan_id="1", repay_amount="1000000")
        assert schema.slippage_bps == "500"  # default


class TestFlashLoanSchemas:
    def test_get_flash_loan_fee(self):
        GetFlashLoanFeeSchema()  # No fields

    def test_estimate_flash_arb_profit(self):
        schema = EstimateFlashArbProfitSchema(
            token=VALID_ADDRESS,
            amount="1000000000",
            legs=[
                {
                    "token_in": VALID_ADDRESS,
                    "token_out": VALID_ADDRESS,
                    "tick_spacing": 100,
                }
            ],
        )
        assert len(schema.legs) == 1
        assert schema.legs[0].amount_out_min == "0"  # default

    def test_estimate_flash_arb_profit_empty_legs(self):
        with pytest.raises(ValidationError):
            EstimateFlashArbProfitSchema(
                token=VALID_ADDRESS,
                amount="1000000000",
                legs=[],
            )

    def test_flash_loan(self):
        schema = FlashLoanSchema(
            token=VALID_ADDRESS,
            amount="1000000000",
            callback_data="0x1234",
        )
        assert schema.callback_data == "0x1234"

    def test_flash_arb(self):
        schema = FlashArbSchema(
            token=VALID_ADDRESS,
            amount="1000000000",
            legs=[
                {
                    "token_in": VALID_ADDRESS,
                    "token_out": VALID_ADDRESS,
                    "tick_spacing": 100,
                }
            ],
        )
        assert schema.min_profit == "0"  # default
        assert schema.deadline is None  # optional
        assert schema.receiver_address is None  # optional

    def test_get_flash_arb_balance(self):
        schema = GetFlashArbBalanceSchema(token=VALID_ADDRESS)
        assert schema.receiver_address is None


class TestDeployVerifySchemas:
    def test_deploy_flash_arb_receiver(self):
        DeployFlashArbReceiverSchema()  # No fields

    def test_check_flash_arb_readiness(self):
        schema = CheckFlashArbReadinessSchema()
        assert schema.receiver_address is None

    def test_check_flash_arb_readiness_with_address(self):
        schema = CheckFlashArbReadinessSchema(receiver_address=VALID_ADDRESS)
        assert schema.receiver_address == VALID_ADDRESS

    def test_verify_flash_arb_receiver(self):
        schema = VerifyFlashArbReceiverSchema()
        assert schema.receiver_address is None
