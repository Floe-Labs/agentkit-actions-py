"""Pydantic schemas for Floe AgentKit action inputs.

Direct Python port of the 23 Zod schemas from the TypeScript AgentKit Actions package.
Each schema corresponds to an action provider method's input validation.
"""

from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Regex patterns for Ethereum primitives
# ---------------------------------------------------------------------------

ADDRESS_PATTERN = re.compile(r"^0x[a-fA-F0-9]{40}$")
BYTES32_PATTERN = re.compile(r"^0x[a-fA-F0-9]{64}$")


# ---------------------------------------------------------------------------
# Reusable validators
# ---------------------------------------------------------------------------


def _validate_address(value: str) -> str:
    if not ADDRESS_PATTERN.match(value):
        raise ValueError("Must be a valid Ethereum address (0x + 40 hex chars)")
    return value


def _validate_bytes32(value: str) -> str:
    if not BYTES32_PATTERN.match(value):
        raise ValueError("Must be a valid bytes32 (0x + 64 hex chars)")
    return value


# ===========================================================================
# Read Action Schemas
# ===========================================================================


class GetMarketsSchema(BaseModel):
    """Input schema for querying Floe lending markets."""

    market_ids: Optional[list[str]] = Field(
        default=None,
        description=(
            "Optional list of market IDs to query. If omitted, uses the provider's "
            "configured known market IDs. Each market represents a unique loan token "
            "+ collateral token pair."
        ),
    )

    @field_validator("market_ids")
    @classmethod
    def validate_market_ids(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is not None:
            for item in v:
                _validate_bytes32(item)
        return v


class GetLoanSchema(BaseModel):
    """Input schema for querying a single Floe loan by ID."""

    loan_id: str = Field(
        description=(
            "The numeric ID of the loan to query. Floe loans are fixed-rate, "
            "fixed-term positions created when a lend intent and borrow intent "
            "are matched."
        ),
    )


class GetMyLoansSchema(BaseModel):
    """Input schema for querying all loans belonging to the connected wallet."""


class CheckLoanHealthSchema(BaseModel):
    """Input schema for checking the health status of a loan."""

    loan_id: str = Field(
        description=(
            "The numeric ID of the loan to check. Returns current LTV, health "
            "status, accrued interest, and distance to liquidation threshold."
        ),
    )


class GetPriceSchema(BaseModel):
    """Input schema for fetching oracle prices for a token pair."""

    collateral_token: str = Field(
        description="The collateral token address. Floe uses Chainlink + Pyth oracles for pricing.",
    )
    loan_token: str = Field(
        description="The loan token address.",
    )

    @field_validator("collateral_token", "loan_token")
    @classmethod
    def validate_address(cls, v: str) -> str:
        return _validate_address(v)


class GetAccruedInterestSchema(BaseModel):
    """Input schema for querying accrued interest on a loan."""

    loan_id: str = Field(
        description=(
            "The numeric ID of the loan. Returns the interest accrued since "
            "origination and time elapsed."
        ),
    )


class GetLiquidationQuoteSchema(BaseModel):
    """Input schema for getting a liquidation quote for a loan."""

    loan_id: str = Field(
        description="The numeric ID of the loan to get a liquidation quote for.",
    )
    repay_amount: str = Field(
        description=(
            "The amount of loan token principal to repay (in raw token units, "
            "e.g. '1000000' for 1 USDC). For full liquidation, use the loan's "
            "full principal."
        ),
    )


class GetIntentBookSchema(BaseModel):
    """Input schema for looking up an on-chain intent by its hash."""

    intent_hash: str = Field(
        description="The on-chain hash of the intent to look up.",
    )
    intent_type: Literal["lend", "borrow"] = Field(
        description="Whether this is a lend intent or borrow intent.",
    )

    @field_validator("intent_hash")
    @classmethod
    def validate_intent_hash(cls, v: str) -> str:
        return _validate_bytes32(v)


# ===========================================================================
# Write Action Schemas
# ===========================================================================


class PostLendIntentSchema(BaseModel):
    """Input schema for posting a lend intent to the Floe protocol."""

    amount: str = Field(
        description=(
            "Total amount to lend in raw token units (e.g. '1000000000' for "
            "1000 USDC with 6 decimals). Unlike Aave/Compound pools, this "
            "creates a fixed-rate offer that gets matched to a specific borrower."
        ),
    )
    min_fill_amount: str = Field(
        description=(
            "Minimum amount per match in raw token units. Set equal to amount "
            "to require full fill."
        ),
    )
    min_interest_rate_bps: str = Field(
        description=(
            "Minimum acceptable annual interest rate in basis points "
            "(e.g. '500' = 5.00%). This is your floor rate."
        ),
    )
    max_ltv_bps: str = Field(
        description=(
            "Maximum LTV ratio in basis points (e.g. '8000' = 80%). This "
            "becomes the liquidation threshold on the resulting loan."
        ),
    )
    min_duration: str = Field(
        description="Minimum loan duration in seconds (e.g. '86400' = 1 day).",
    )
    max_duration: str = Field(
        description="Maximum loan duration in seconds (e.g. '2592000' = 30 days).",
    )
    market_id: str = Field(
        description="The market ID (identifies the loan token + collateral token pair).",
    )
    allow_partial_fill: bool = Field(
        default=True,
        description="Whether the intent can be partially filled.",
    )
    expiry_seconds: str = Field(
        default="86400",
        description=(
            "How long the intent remains valid, in seconds from now "
            "(default: 86400 = 24 hours)."
        ),
    )
    grace_period: str = Field(
        default="86400",
        description=(
            "Grace period in seconds after loan duration expires before "
            "overdue liquidation (default: 86400 = 24 hours)."
        ),
    )
    min_interest_bps: str = Field(
        default="0",
        description=(
            "Minimum interest as basis points of full-term interest, enforced "
            "on early repayment (0 = no minimum)."
        ),
    )

    @field_validator("market_id")
    @classmethod
    def validate_market_id(cls, v: str) -> str:
        return _validate_bytes32(v)


class PostBorrowIntentSchema(BaseModel):
    """Input schema for posting a borrow intent to the Floe protocol."""

    borrow_amount: str = Field(
        description="Amount to borrow in raw token units.",
    )
    collateral_amount: str = Field(
        description="Amount of collateral to lock in raw token units.",
    )
    min_fill_amount: str = Field(
        description="Minimum borrow amount per match in raw token units.",
    )
    max_interest_rate_bps: str = Field(
        description=(
            "Maximum acceptable annual interest rate in basis points "
            "(e.g. '1000' = 10.00%)."
        ),
    )
    min_ltv_bps: str = Field(
        description=(
            "Minimum LTV ratio in basis points for the actual loan "
            "(e.g. '5000' = 50%)."
        ),
    )
    min_duration: str = Field(
        description="Minimum loan duration in seconds.",
    )
    max_duration: str = Field(
        description="Maximum loan duration in seconds.",
    )
    market_id: str = Field(
        description="The market ID (identifies the loan token + collateral token pair).",
    )
    allow_partial_fill: bool = Field(
        default=False,
        description="Whether the intent can be partially filled.",
    )
    matcher_commission_bps: str = Field(
        default="50",
        description=(
            "Commission paid to the solver/matcher bot in basis points "
            "(default: 50 = 0.50%)."
        ),
    )
    expiry_seconds: str = Field(
        default="86400",
        description=(
            "How long the intent remains valid, in seconds from now "
            "(default: 86400 = 24 hours)."
        ),
    )
    on_behalf_of: Optional[str] = Field(
        default=None,
        description=(
            "Optional address to receive loan proceeds instead of your wallet. "
            "If omitted, USDC is sent to your address."
        ),
    )

    @field_validator("market_id")
    @classmethod
    def validate_market_id(cls, v: str) -> str:
        return _validate_bytes32(v)

    @field_validator("on_behalf_of")
    @classmethod
    def validate_on_behalf_of(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _validate_address(v)
        return v


class MatchIntentsSchema(BaseModel):
    """Input schema for matching a lend intent with a borrow intent."""

    lend_intent_hash: str = Field(
        description="The on-chain hash of the lend intent to match.",
    )
    borrow_intent_hash: str = Field(
        description="The on-chain hash of the borrow intent to match.",
    )
    market_id: str = Field(
        description="The market ID both intents must belong to.",
    )

    @field_validator("lend_intent_hash", "borrow_intent_hash", "market_id")
    @classmethod
    def validate_bytes32(cls, v: str) -> str:
        return _validate_bytes32(v)


class RepayLoanSchema(BaseModel):
    """Input schema for repaying a Floe loan."""

    loan_id: str = Field(
        description="The numeric ID of the loan to repay.",
    )
    repay_amount: str = Field(
        description="The principal amount to repay in raw token units.",
    )
    slippage_bps: str = Field(
        default="500",
        description=(
            "Slippage tolerance in basis points for maxTotalRepayment "
            "calculation (default: 500 = 5%)."
        ),
    )


class AddCollateralSchema(BaseModel):
    """Input schema for adding collateral to an existing loan."""

    loan_id: str = Field(
        description="The numeric ID of the loan to add collateral to.",
    )
    amount: str = Field(
        description="Amount of collateral token to add in raw token units.",
    )


class WithdrawCollateralSchema(BaseModel):
    """Input schema for withdrawing collateral from a loan."""

    loan_id: str = Field(
        description=(
            "The numeric ID of the loan to withdraw collateral from. "
            "Only the borrower can call this."
        ),
    )
    amount: str = Field(
        description=(
            "Amount of collateral token to withdraw in raw token units. "
            "The resulting LTV must stay below the liquidation threshold "
            "minus a 3% buffer."
        ),
    )


class LiquidateLoanSchema(BaseModel):
    """Input schema for liquidating an unhealthy loan."""

    loan_id: str = Field(
        description="The numeric ID of the loan to liquidate.",
    )
    repay_amount: str = Field(
        description="The principal amount to repay for the liquidation in raw token units.",
    )
    slippage_bps: str = Field(
        default="500",
        description="Slippage tolerance in basis points (default: 500 = 5%).",
    )


# ===========================================================================
# Flash Loan Schemas
# ===========================================================================


class GetFlashLoanFeeSchema(BaseModel):
    """Input schema for querying the protocol's flash loan fee."""


class EstimateLegSchema(BaseModel):
    """A single swap leg for flash arbitrage profit estimation."""

    token_in: str = Field(
        description="Input token address for this swap leg.",
    )
    token_out: str = Field(
        description="Output token address for this swap leg.",
    )
    tick_spacing: int = Field(
        description="Aerodrome pool tick spacing (e.g. 1, 100, 200).",
    )
    amount_out_min: str = Field(
        default="0",
        description="Minimum output for this leg in raw units. Use '0' to skip slippage check.",
    )

    @field_validator("token_in", "token_out")
    @classmethod
    def validate_address(cls, v: str) -> str:
        return _validate_address(v)


class EstimateFlashArbProfitSchema(BaseModel):
    """Input schema for estimating profit from a flash arbitrage route."""

    token: str = Field(
        description="The token to flash-borrow.",
    )
    amount: str = Field(
        description="The amount to flash-borrow in raw token units.",
    )
    legs: list[EstimateLegSchema] = Field(
        min_length=1,
        description="Ordered array of swap legs.",
    )

    @field_validator("token")
    @classmethod
    def validate_token(cls, v: str) -> str:
        return _validate_address(v)


class FlashLoanSchema(BaseModel):
    """Input schema for executing a raw flash loan from Floe's lending pool."""

    token: str = Field(
        description="The token to flash-borrow from Floe's lending pool.",
    )
    amount: str = Field(
        description="The amount to flash-borrow in raw token units.",
    )
    callback_data: str = Field(
        description="ABI-encoded bytes to pass to the receiver's receiveFlashLoan callback.",
    )

    @field_validator("token")
    @classmethod
    def validate_token(cls, v: str) -> str:
        return _validate_address(v)


class FlashArbLegSchema(BaseModel):
    """A single swap leg for a flash arbitrage execution."""

    token_in: str = Field(
        description="Input token for this swap leg.",
    )
    token_out: str = Field(
        description="Output token for this swap leg.",
    )
    tick_spacing: int = Field(
        description="Aerodrome pool tick spacing (e.g. 1, 100, 200). Use 0 for multi-hop legs.",
    )
    amount_in: str = Field(
        default="0",
        description="Amount of tokenIn to swap. Use '0' to swap entire balance.",
    )
    min_amount_out: str = Field(
        default="0",
        description="Minimum acceptable output (slippage protection).",
    )
    is_multi_hop: bool = Field(
        default=False,
        description="If true, uses multi-hop routing with the encoded path.",
    )
    path: str = Field(
        default="0x",
        description="ABI-encoded multi-hop path for Aerodrome exactInput.",
    )

    @field_validator("token_in", "token_out")
    @classmethod
    def validate_address(cls, v: str) -> str:
        return _validate_address(v)


class FlashArbSchema(BaseModel):
    """Input schema for executing a flash arbitrage via Floe flash loans."""

    token: str = Field(
        description="The token to flash-borrow for the arbitrage.",
    )
    amount: str = Field(
        description="The amount to flash-borrow in raw token units.",
    )
    receiver_address: Optional[str] = Field(
        default=None,
        description=(
            "The deployed FlashArbReceiver contract address. If omitted, uses "
            "the address from the most recent deploy_flash_arb_receiver call "
            "in this session."
        ),
    )
    legs: list[FlashArbLegSchema] = Field(
        min_length=1,
        description="Ordered array of swap legs for the arbitrage route.",
    )
    min_profit: str = Field(
        default="0",
        description="Minimum profit in raw token units after repaying the flash loan + fee.",
    )
    deadline: Optional[str] = Field(
        default=None,
        description="Unix timestamp after which the swap transactions revert.",
    )

    @field_validator("token", "receiver_address")
    @classmethod
    def validate_address(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _validate_address(v)
        return v


class GetFlashArbBalanceSchema(BaseModel):
    """Input schema for checking a token balance held by a FlashArbReceiver contract."""

    token: str = Field(
        description="The ERC20 token address to check the balance of.",
    )
    receiver_address: Optional[str] = Field(
        default=None,
        description="The FlashArbReceiver contract address.",
    )

    @field_validator("token", "receiver_address")
    @classmethod
    def validate_address(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _validate_address(v)
        return v


# ===========================================================================
# Deploy / Verify / Readiness Schemas
# ===========================================================================


class DeployFlashArbReceiverSchema(BaseModel):
    """Input schema for deploying a new FlashArbReceiver contract."""


class CheckFlashArbReadinessSchema(BaseModel):
    """Input schema for checking flash arbitrage readiness and configuration."""

    receiver_address: Optional[str] = Field(
        default=None,
        description=(
            "Optional FlashArbReceiver address. If provided, also verifies "
            "the receiver's immutables and owner."
        ),
    )

    @field_validator("receiver_address")
    @classmethod
    def validate_receiver_address(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _validate_address(v)
        return v


class VerifyFlashArbReceiverSchema(BaseModel):
    """Input schema for verifying an existing FlashArbReceiver contract."""

    receiver_address: Optional[str] = Field(
        default=None,
        description=(
            "The FlashArbReceiver contract address to verify. If omitted, uses "
            "the address from the most recent deploy_flash_arb_receiver call "
            "in this session."
        ),
    )

    @field_validator("receiver_address")
    @classmethod
    def validate_receiver_address(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _validate_address(v)
        return v
