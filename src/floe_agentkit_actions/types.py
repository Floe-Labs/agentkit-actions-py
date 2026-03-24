"""Domain models for the Floe lending protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Condition:
    target: str
    call_data: str
    apply_to_all_partial_fills: bool


@dataclass
class Hook:
    target: str
    call_data: str
    gas_limit: int
    expiry: int
    allow_failure: bool
    apply_to_all_partial_fills: bool


@dataclass
class PauseStatuses:
    is_add_collateral_paused: bool
    is_borrow_paused: bool
    is_withdraw_collateral_paused: bool
    is_repay_paused: bool
    is_liquidate_paused: bool


@dataclass
class Market:
    market_id: str
    loan_token: str
    collateral_token: str
    interest_rate_bps: int
    ltv_bps: int
    liquidation_incentive_bps: int
    market_fee_bps: int
    total_principal_outstanding: int
    total_loans: int
    last_update_at: int
    pause_statuses: PauseStatuses


@dataclass
class Loan:
    market_id: str
    loan_id: int
    lender: str
    borrower: str
    loan_token: str
    collateral_token: str
    principal: int
    interest_rate_bps: int
    ltv_bps: int
    liquidation_ltv_bps: int
    market_fee_bps: int
    matcher_commission_bps: int
    start_time: int
    duration: int
    collateral_amount: int
    repaid: bool
    grace_period: int
    min_interest_bps: int


@dataclass
class LendIntent:
    lender: str
    on_behalf_of: str
    amount: int
    min_fill_amount: int
    filled_amount: int
    min_interest_rate_bps: int
    max_ltv_bps: int
    min_duration: int
    max_duration: int
    allow_partial_fill: bool
    valid_from_timestamp: int
    expiry: int
    market_id: str
    salt: str
    grace_period: int
    min_interest_bps: int
    conditions: list[Condition] = field(default_factory=list)
    pre_hooks: list[Hook] = field(default_factory=list)
    post_hooks: list[Hook] = field(default_factory=list)


@dataclass
class BorrowIntent:
    borrower: str
    on_behalf_of: str
    borrow_amount: int
    collateral_amount: int
    min_fill_amount: int
    max_interest_rate_bps: int
    min_ltv_bps: int
    min_duration: int
    max_duration: int
    allow_partial_fill: bool
    valid_from_timestamp: int
    matcher_commission_bps: int
    expiry: int
    market_id: str
    salt: str
    conditions: list[Condition] = field(default_factory=list)
    pre_hooks: list[Hook] = field(default_factory=list)
    post_hooks: list[Hook] = field(default_factory=list)


@dataclass
class LiquidationQuote:
    loan_id: int
    is_underwater: bool
    requires_full_liquidation: bool
    repay_amount: int
    interest_amount: int
    total_liquidator_pays: int
    collateral_to_receive: int
    collateral_value_received: int
    lender_receives: int
    protocol_fee_amount: int
    liquidator_profit: int
    liquidator_profit_bps: int
    bad_debt_amount: int


@dataclass
class FloeConfig:
    lending_intent_matcher_address: Optional[str] = None
    lending_views_address: Optional[str] = None
    known_market_ids: list[str] = field(default_factory=list)


@dataclass
class ArbLeg:
    is_multi_hop: bool
    tick_spacing: int
    token_in: str
    token_out: str
    amount_in: int
    min_amount_out: int
    path: str


@dataclass
class ArbParams:
    legs: list[ArbLeg]
    min_profit: int
    deadline: int
