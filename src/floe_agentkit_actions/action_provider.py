"""Floe Lending Protocol action provider for Coinbase AgentKit (Python).

Complete port of the TypeScript ``floeActionProvider.ts`` with all 23 actions
for interacting with Floe's intent-based lending protocol, flash loans, and
flash arbitrage on Base mainnet / Base Sepolia.
"""

from __future__ import annotations

import math
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Optional

from coinbase_agentkit import ActionProvider, create_action
from coinbase_agentkit.network import Network
from coinbase_agentkit.wallet_providers import EvmWalletProvider
from eth_abi import encode as eth_abi_encode
from web3 import Web3

from .constants import (
    AERODROME_QUOTER_V2_ABI,
    AERODROME_QUOTER_V2_ADDRESS,
    AERODROME_SWAP_ROUTER_ADDRESS,
    BASE_MAINNET_MATCHER,
    BASE_MAINNET_ORACLE,
    BASE_MAINNET_VIEWS,
    BASE_WETH_ADDRESS,
    BASIS_POINTS,
    ERC20_ABI,
    FLASH_ARB_RECEIVER_ABI,
    LENDING_MATCHER_ABI,
    LENDING_VIEWS_ABI,
    LOG_INTENTS_MATCHED_DETAILED_EVENT,
    LOG_LENDER_OFFER_POSTED_EVENT,
    MATCHER_DEPLOYMENT_BLOCK,
    ORACLE_PRICE_SCALE,
    PRICE_ORACLE_ABI,
)
from .flash_arb_bytecode import (
    FLASH_ARB_RECEIVER_BYTECODE,
    FLASH_ARB_RECEIVER_CONSTRUCTOR_ABI,
)
from .schemas import (
    AddCollateralSchema,
    CheckCreditStatusSchema,
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
    InstantBorrowSchema,
    LiquidateLoanSchema,
    ManualMatchCreditSchema,
    MatchIntentsSchema,
    PostBorrowIntentSchema,
    PostLendIntentSchema,
    RenewCreditLineSchema,
    RepayAndReborrowSchema,
    RepayCreditSchema,
    RepayLoanSchema,
    RequestCreditSchema,
    VerifyFlashArbReceiverSchema,
    WithdrawCollateralSchema,
)
from .types import FloeConfig
from .utils import (
    compute_health_percent,
    format_address,
    format_bps,
    format_duration,
    format_price,
    format_timestamp,
    format_token_amount,
    resolve_token_meta,
)

# ---------------------------------------------------------------------------
# Shared web3 instance for ABI encoding only (no provider needed)
# ---------------------------------------------------------------------------
_w3 = Web3()

# ABI for the SwapRouter factory() check
_SWAP_ROUTER_FACTORY_ABI: list[dict] = [
    {
        "type": "function",
        "name": "factory",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
]


class FloeActionProvider(ActionProvider[EvmWalletProvider]):
    """Action provider exposing 23 on-chain actions for Floe lending protocol."""

    def __init__(self, config: Optional[FloeConfig] = None) -> None:
        super().__init__("floe", [])
        cfg = config or FloeConfig()
        self._matcher_address: str = cfg.lending_intent_matcher_address or BASE_MAINNET_MATCHER
        self._views_address: str = cfg.lending_views_address or BASE_MAINNET_VIEWS
        self._known_market_ids: list[str] = cfg.known_market_ids or []
        self._rpc_url: Optional[str] = cfg.rpc_url
        self._offer_scan_lookback_blocks: Optional[int] = cfg.offer_scan_lookback_blocks
        self._deployed_receiver_address: Optional[str] = None
        self._public_client: Optional[Web3] = None

    def supports_network(self, network: Network) -> bool:
        return network.chain_id in ("8453", "84532")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_receiver_address(self, provided_address: Optional[str] = None) -> str:
        """Return *provided_address* or the last deployed receiver, raising if neither."""
        addr = provided_address or self._deployed_receiver_address
        if not addr:
            raise ValueError(
                "No receiver address provided and no receiver deployed in this session. "
                "Provide a receiverAddress or run deploy_flash_arb_receiver first."
            )
        return addr

    def _ensure_allowance(
        self,
        wallet_provider: EvmWalletProvider,
        token_address: str,
        spender_address: str,
        required_amount: int,
    ) -> Optional[str]:
        """Check current allowance; if insufficient, approve *required_amount*.

        Returns a human-readable approval message or ``None`` if no approval was needed.
        """
        owner = wallet_provider.get_address()
        current_allowance = wallet_provider.read_contract(
            contract_address=token_address,
            abi=ERC20_ABI,
            function_name="allowance",
            args=[owner, spender_address],
        )
        current_allowance = int(current_allowance)

        if current_allowance >= required_amount:
            return None

        contract = _w3.eth.contract(abi=ERC20_ABI)
        encoded = contract.encode_abi("approve", args=[spender_address, required_amount])

        tx_hash = wallet_provider.send_transaction(transaction={"to": token_address, "data": encoded})

        meta = resolve_token_meta(token_address, wallet_provider)
        return (
            f"Approved {format_token_amount(required_amount, meta['decimals'], meta['symbol'])} "
            f"to {format_address(spender_address)} (tx: {tx_hash})"
        )

    # ==================================================================
    # READ ACTIONS (1-8)
    # ==================================================================

    @create_action(
        name="get_markets",
        description=(
            "Get information about Floe lending markets. Each market represents a unique "
            "loan token + collateral token pair with its own interest rate floor, LTV limits, "
            "and liquidation incentive. Unlike Aave/Compound pool-based lending, Floe markets "
            "are intent-based — lenders and borrowers post offers that get matched at fixed "
            "rates and terms."
        ),
        schema=GetMarketsSchema,
    )
    def get_markets(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            ids = args.get("market_ids") or self._known_market_ids
            if not ids:
                return (
                    "No market IDs provided and no known markets configured. "
                    "Pass marketIds or configure knownMarketIds in the provider constructor."
                )

            lines: list[str] = ["## Floe Lending Markets\n"]

            for i, market_id in enumerate(ids):
                m = wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getMarket",
                    args=[market_id],
                )

                loan_meta = resolve_token_meta(m.loanToken, wallet_provider)
                coll_meta = resolve_token_meta(m.collateralToken, wallet_provider)

                lines.append(f"### Market: {coll_meta['symbol']}/{loan_meta['symbol']}")
                lines.append(f"- **Market ID**: {ids[i]}")
                lines.append(f"- **Loan Token**: {loan_meta['symbol']} ({m.loanToken})")
                lines.append(f"- **Collateral Token**: {coll_meta['symbol']} ({m.collateralToken})")
                lines.append(f"- **Min Interest Rate**: {format_bps(int(m.interestRateBps))}")
                lines.append(f"- **Min LTV**: {format_bps(int(m.ltvBps))}")
                lines.append(f"- **Liquidation Incentive**: {format_bps(int(m.liquidationIncentiveBps))}")
                lines.append(f"- **Market Fee**: {format_bps(int(m.marketFeeBps))}")
                lines.append(
                    f"- **Total Outstanding**: "
                    f"{format_token_amount(int(m.totalPrincipalOutstanding), loan_meta['decimals'], loan_meta['symbol'])}"
                )
                lines.append(f"- **Total Loans Created**: {int(m.totalLoans)}")

                pauses: list[str] = []
                ps = m.pauseStatuses
                if ps.isBorrowPaused:
                    pauses.append("borrowing")
                if ps.isRepayPaused:
                    pauses.append("repayment")
                if ps.isLiquidatePaused:
                    pauses.append("liquidation")
                if ps.isAddCollateralPaused:
                    pauses.append("add collateral")
                if ps.isWithdrawCollateralPaused:
                    pauses.append("withdraw collateral")

                if pauses:
                    lines.append(f"- **Paused**: {', '.join(pauses)}")
                else:
                    lines.append("- **Status**: All operations active")
                lines.append("")

            return "\n".join(lines)
        except Exception as e:
            return f"Error fetching markets: {e}"

    @create_action(
        name="get_loan",
        description=(
            "Get detailed information about a specific Floe loan. Returns the loan's "
            "terms (rate, LTV, duration), current health status, accrued interest, and "
            "participant addresses."
        ),
        schema=GetLoanSchema,
    )
    def get_loan(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            loan_id = int(args["loan_id"])

            loan = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoan",
                args=[loan_id],
            )
            current_ltv = int(
                wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getCurrentLtvBps",
                    args=[loan_id],
                )
            )
            healthy = bool(
                wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="isHealthy",
                    args=[loan_id],
                )
            )
            interest_data = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getAccruedInterest",
                args=[loan_id],
            )
            interest_amount = int(interest_data[0])
            time_elapsed = int(interest_data[1])

            loan_meta = resolve_token_meta(loan.loanToken, wallet_provider)
            coll_meta = resolve_token_meta(loan.collateralToken, wallet_provider)

            end_time = int(loan.startTime) + int(loan.duration)
            now = int(time.time())
            is_overdue = now > end_time + int(loan.gracePeriod)
            time_remaining = end_time - now if end_time > now else 0

            if loan.repaid:
                status = "Repaid"
            elif healthy:
                status = "Healthy"
            else:
                status = "UNHEALTHY -- Liquidatable"

            if loan.repaid:
                remaining_str = "N/A"
            elif time_remaining > 0:
                remaining_str = format_duration(time_remaining)
            elif is_overdue:
                remaining_str = "OVERDUE"
            else:
                remaining_str = "Expired (in grace period)"

            lines = [
                f"## Loan #{args['loan_id']}\n",
                f"- **Status**: {status}",
                f"- **Lender**: {format_address(loan.lender)}",
                f"- **Borrower**: {format_address(loan.borrower)}",
                f"- **Principal**: {format_token_amount(int(loan.principal), loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Collateral**: {format_token_amount(int(loan.collateralAmount), coll_meta['decimals'], coll_meta['symbol'])}",
                f"- **Interest Rate**: {format_bps(int(loan.interestRateBps))} annual",
                f"- **Accrued Interest**: {format_token_amount(interest_amount, loan_meta['decimals'], loan_meta['symbol'])} ({format_duration(time_elapsed)} elapsed)",
                f"- **Origination LTV**: {format_bps(int(loan.ltvBps))}",
                f"- **Current LTV**: {format_bps(current_ltv)}",
                f"- **Liquidation LTV**: {format_bps(int(loan.liquidationLtvBps))}",
                f"- **Health Buffer**: {compute_health_percent(current_ltv, int(loan.liquidationLtvBps))}",
                f"- **Start**: {format_timestamp(int(loan.startTime))}",
                f"- **Duration**: {format_duration(int(loan.duration))}",
                f"- **Time Remaining**: {remaining_str}",
                f"- **Grace Period**: {format_duration(int(loan.gracePeriod))}",
                f"- **Market Fee**: {format_bps(int(loan.marketFeeBps))}",
                f"- **Matcher Commission**: {format_bps(int(loan.matcherCommissionBps))}",
                f"- **Min Interest Bps**: {format_bps(int(loan.minInterestBps))}",
                f"- **Market ID**: {loan.marketId}",
            ]

            return "\n".join(lines)
        except Exception as e:
            return f"Error fetching loan: {e}"

    @create_action(
        name="get_my_loans",
        description=(
            "Get all loans associated with the connected wallet (as lender or borrower). "
            "Returns a summary of each loan's status, amounts, and health."
        ),
        schema=GetMyLoansSchema,
    )
    def get_my_loans(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            user_address = wallet_provider.get_address()

            loan_ids = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoanIdsByUser",
                args=[user_address],
            )

            if not loan_ids:
                return f"No loans found for {format_address(user_address)}."

            lines = [f"## My Loans ({format_address(user_address)})\n"]
            lines.append(f"Found {len(loan_ids)} loan(s).\n")

            for i, lid in enumerate(loan_ids):
                loan = wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getLoan",
                    args=[int(lid)],
                )
                loan_meta = resolve_token_meta(loan.loanToken, wallet_provider)
                coll_meta = resolve_token_meta(loan.collateralToken, wallet_provider)
                role = (
                    "Lender"
                    if str(loan.lender).lower() == str(user_address).lower()
                    else "Borrower"
                )
                status = "Repaid" if loan.repaid else "Active"

                lines.append(
                    f"**Loan #{int(lid)}** -- {role} | {status} | "
                    f"{format_token_amount(int(loan.principal), loan_meta['decimals'], loan_meta['symbol'])} -> "
                    f"{format_token_amount(int(loan.collateralAmount), coll_meta['decimals'], coll_meta['symbol'])} | "
                    f"Rate: {format_bps(int(loan.interestRateBps))}"
                )

            return "\n".join(lines)
        except Exception as e:
            return f"Error fetching loans: {e}"

    @create_action(
        name="check_loan_health",
        description=(
            "Check the health status of a Floe loan. Returns current LTV vs liquidation "
            "threshold, accrued interest, time remaining, and whether the loan is at risk "
            "of liquidation."
        ),
        schema=CheckLoanHealthSchema,
    )
    def check_loan_health(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            loan_id = int(args["loan_id"])

            loan = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoan",
                args=[loan_id],
            )
            current_ltv = int(
                wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getCurrentLtvBps",
                    args=[loan_id],
                )
            )
            healthy = bool(
                wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="isHealthy",
                    args=[loan_id],
                )
            )
            interest_data = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getAccruedInterest",
                args=[loan_id],
            )
            interest_amount = int(interest_data[0])
            time_elapsed = int(interest_data[1])

            if loan.repaid:
                return f"Loan #{args['loan_id']} has been fully repaid. No health check needed."

            loan_meta = resolve_token_meta(loan.loanToken, wallet_provider)

            end_time = int(loan.startTime) + int(loan.duration)
            now = int(time.time())
            time_remaining = end_time - now if end_time > now else 0
            is_overdue = now > end_time + int(loan.gracePeriod)

            distance_bps = int(loan.liquidationLtvBps) - current_ltv
            total_debt = int(loan.principal) + interest_amount

            if time_remaining > 0:
                remaining_str = format_duration(time_remaining)
            elif is_overdue:
                remaining_str = "OVERDUE -- can be liquidated"
            else:
                remaining_str = "Expired (in grace period)"

            lines = [
                f"## Health Check -- Loan #{args['loan_id']}\n",
                f"- **Healthy**: {'Yes' if healthy else 'NO -- Liquidatable!'}",
                f"- **Current LTV**: {format_bps(current_ltv)}",
                f"- **Liquidation LTV**: {format_bps(int(loan.liquidationLtvBps))}",
                f"- **Distance to Liquidation**: {format_bps(distance_bps)} ({compute_health_percent(current_ltv, int(loan.liquidationLtvBps))} buffer)",
                f"- **Total Debt**: {format_token_amount(total_debt, loan_meta['decimals'], loan_meta['symbol'])} (principal + interest)",
                f"- **Accrued Interest**: {format_token_amount(interest_amount, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Time Remaining**: {remaining_str}",
            ]

            if not healthy:
                lines.append(
                    "\n**Action Required**: This loan can be liquidated. "
                    "The borrower should repay or add collateral immediately."
                )
            elif distance_bps < 500:
                lines.append(
                    "\n**Warning**: This loan is close to the liquidation threshold. "
                    "Consider adding collateral."
                )

            return "\n".join(lines)
        except Exception as e:
            return f"Error checking loan health: {e}"

    @create_action(
        name="get_price",
        description=(
            "Get the oracle price for a collateral/loan token pair from Floe's price oracle "
            "(Chainlink primary + Pyth fallback)."
        ),
        schema=GetPriceSchema,
    )
    def get_price(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            price = int(
                wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getPrice",
                    args=[args["collateral_token"], args["loan_token"]],
                )
            )

            coll_meta = resolve_token_meta(args["collateral_token"], wallet_provider)
            loan_meta = resolve_token_meta(args["loan_token"], wallet_provider)

            return "\n".join([
                "## Oracle Price\n",
                f"- **Pair**: {coll_meta['symbol']} / {loan_meta['symbol']}",
                f"- **Price**: 1 {coll_meta['symbol']} = {format_price(price)} {loan_meta['symbol']}",
                f"- **Raw Price (36-decimal scale)**: {price}",
            ])
        except Exception as e:
            return f"Error fetching price: {e}"

    @create_action(
        name="get_accrued_interest",
        description=(
            "Get the accrued interest on a Floe loan. Returns the interest amount and time "
            "elapsed since loan origination."
        ),
        schema=GetAccruedInterestSchema,
    )
    def get_accrued_interest(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            loan_id = int(args["loan_id"])

            interest_data = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getAccruedInterest",
                args=[loan_id],
            )
            loan = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoan",
                args=[loan_id],
            )

            interest_amount = int(interest_data[0])
            time_elapsed = int(interest_data[1])
            loan_meta = resolve_token_meta(loan.loanToken, wallet_provider)

            return "\n".join([
                f"## Accrued Interest -- Loan #{args['loan_id']}\n",
                f"- **Interest**: {format_token_amount(interest_amount, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Time Elapsed**: {format_duration(time_elapsed)}",
                f"- **Interest Rate**: {format_bps(int(loan.interestRateBps))} annual",
                f"- **Principal**: {format_token_amount(int(loan.principal), loan_meta['decimals'], loan_meta['symbol'])}",
            ])
        except Exception as e:
            return f"Error fetching accrued interest: {e}"

    @create_action(
        name="get_liquidation_quote",
        description=(
            "Get a liquidation quote for a Floe loan. Shows the profit/loss breakdown, "
            "collateral to receive, and whether the loan is underwater. Useful for evaluating "
            "liquidation opportunities."
        ),
        schema=GetLiquidationQuoteSchema,
    )
    def get_liquidation_quote(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            loan_id = int(args["loan_id"])
            repay_amount = int(args["repay_amount"])

            quote = wallet_provider.read_contract(
                contract_address=self._views_address,
                abi=LENDING_VIEWS_ABI,
                function_name="getLiquidationQuote",
                args=[loan_id, repay_amount],
            )
            loan = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoan",
                args=[loan_id],
            )

            loan_meta = resolve_token_meta(loan.loanToken, wallet_provider)
            coll_meta = resolve_token_meta(loan.collateralToken, wallet_provider)

            lines = [
                f"## Liquidation Quote -- Loan #{args['loan_id']}\n",
                f"- **Underwater**: {'Yes -- bad debt scenario' if quote.isUnderwater else 'No -- solvent liquidation'}",
                f"- **Requires Full Liquidation**: {'Yes' if quote.requiresFullLiquidation else 'No'}",
                f"- **Repay Amount**: {format_token_amount(int(quote.repayAmount), loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Interest Amount**: {format_token_amount(int(quote.interestAmount), loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Total Liquidator Pays**: {format_token_amount(int(quote.totalLiquidatorPays), loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Collateral to Receive**: {format_token_amount(int(quote.collateralToReceive), coll_meta['decimals'], coll_meta['symbol'])}",
                f"- **Collateral Value**: {format_token_amount(int(quote.collateralValueReceived), loan_meta['decimals'], loan_meta['symbol'])} (in {loan_meta['symbol']} terms)",
                f"- **Lender Receives**: {format_token_amount(int(quote.lenderReceives), loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Protocol Fee**: {format_token_amount(int(quote.protocolFeeAmount), loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Liquidator Profit**: {format_token_amount(int(quote.liquidatorProfit), loan_meta['decimals'], loan_meta['symbol'])} ({format_bps(int(quote.liquidatorProfitBps))})",
            ]

            if int(quote.badDebtAmount) > 0:
                lines.append(
                    f"- **Bad Debt**: {format_token_amount(int(quote.badDebtAmount), loan_meta['decimals'], loan_meta['symbol'])}"
                )

            return "\n".join(lines)
        except Exception as e:
            return f"Error fetching liquidation quote: {e}"

    @create_action(
        name="get_intent_book",
        description=(
            "Look up an on-chain lend or borrow intent by its hash. Returns the full intent "
            "details including amounts, rates, duration, and whether it has been filled."
        ),
        schema=GetIntentBookSchema,
    )
    def get_intent_book(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            intent_hash = args["intent_hash"]
            intent_type = args["intent_type"]
            zero_address = "0x0000000000000000000000000000000000000000"

            if intent_type == "lend":
                intent = wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getOnChainLendIntent",
                    args=[intent_hash],
                )

                if intent.lender == zero_address:
                    return f"No on-chain lend intent found for hash {intent_hash}."

                market = wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getMarket",
                    args=[intent.marketId],
                )
                loan_meta = resolve_token_meta(market.loanToken, wallet_provider)

                remaining = int(intent.amount) - int(intent.filledAmount)

                return "\n".join([
                    "## Lend Intent\n",
                    f"- **Hash**: {intent_hash}",
                    f"- **Lender**: {format_address(intent.lender)}",
                    f"- **Total Amount**: {format_token_amount(int(intent.amount), loan_meta['decimals'], loan_meta['symbol'])}",
                    f"- **Filled**: {format_token_amount(int(intent.filledAmount), loan_meta['decimals'], loan_meta['symbol'])}",
                    f"- **Remaining**: {format_token_amount(remaining, loan_meta['decimals'], loan_meta['symbol'])}",
                    f"- **Min Fill**: {format_token_amount(int(intent.minFillAmount), loan_meta['decimals'], loan_meta['symbol'])}",
                    f"- **Min Interest Rate**: {format_bps(int(intent.minInterestRateBps))}",
                    f"- **Max LTV**: {format_bps(int(intent.maxLtvBps))}",
                    f"- **Duration**: {format_duration(int(intent.minDuration))} -- {format_duration(int(intent.maxDuration))}",
                    f"- **Partial Fill**: {'Yes' if intent.allowPartialFill else 'No'}",
                    f"- **Expiry**: {format_timestamp(int(intent.expiry))}",
                    f"- **Grace Period**: {format_duration(int(intent.gracePeriod))}",
                    f"- **Market ID**: {intent.marketId}",
                ])
            else:
                # borrow
                intent = wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getOnChainBorrowIntent",
                    args=[intent_hash],
                )

                if intent.borrower == zero_address:
                    return f"No on-chain borrow intent found for hash {intent_hash}."

                market = wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getMarket",
                    args=[intent.marketId],
                )

                loan_meta = resolve_token_meta(market.loanToken, wallet_provider)
                coll_meta = resolve_token_meta(market.collateralToken, wallet_provider)

                return "\n".join([
                    "## Borrow Intent\n",
                    f"- **Hash**: {intent_hash}",
                    f"- **Borrower**: {format_address(intent.borrower)}",
                    f"- **Borrow Amount**: {format_token_amount(int(intent.borrowAmount), loan_meta['decimals'], loan_meta['symbol'])}",
                    f"- **Collateral**: {format_token_amount(int(intent.collateralAmount), coll_meta['decimals'], coll_meta['symbol'])}",
                    f"- **Min Fill**: {format_token_amount(int(intent.minFillAmount), loan_meta['decimals'], loan_meta['symbol'])}",
                    f"- **Max Interest Rate**: {format_bps(int(intent.maxInterestRateBps))}",
                    f"- **Min LTV**: {format_bps(int(intent.minLtvBps))}",
                    f"- **Duration**: {format_duration(int(intent.minDuration))} -- {format_duration(int(intent.maxDuration))}",
                    f"- **Partial Fill**: {'Yes' if intent.allowPartialFill else 'No'}",
                    f"- **Matcher Commission**: {format_bps(int(intent.matcherCommissionBps))}",
                    f"- **Expiry**: {format_timestamp(int(intent.expiry))}",
                    f"- **Market ID**: {intent.marketId}",
                ])
        except Exception as e:
            return f"Error fetching intent: {e}"

    # ==================================================================
    # WRITE ACTIONS (9-15)
    # ==================================================================

    @create_action(
        name="post_lend_intent",
        description=(
            "Post a lend intent on Floe. This registers your willingness to lend at a fixed "
            "rate and terms. Unlike Aave/Compound where you deposit into a pool, Floe matches "
            "your intent to a specific borrower. The loan token is automatically approved "
            "before posting."
        ),
        schema=PostLendIntentSchema,
    )
    def post_lend_intent(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            user_address = wallet_provider.get_address()
            now = int(time.time())
            expiry = now + int(args.get("expiry_seconds", "86400"))
            salt = "0x" + secrets.token_hex(32)

            parsed_amount = int(args["amount"])

            # Fetch market to resolve loan token for approval
            market = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getMarket",
                args=[args["market_id"]],
            )

            # Auto-approve loan token with 1% buffer
            approval_amount = (parsed_amount * 101) // 100
            approval_result = self._ensure_allowance(
                wallet_provider,
                market.loanToken,
                self._matcher_address,
                approval_amount,
            )

            intent_struct = (
                user_address,                            # lender
                user_address,                            # onBehalfOf
                parsed_amount,                           # amount
                int(args["min_fill_amount"]),             # minFillAmount
                0,                                       # filledAmount
                int(args["min_interest_rate_bps"]),       # minInterestRateBps
                int(args["max_ltv_bps"]),                 # maxLtvBps
                int(args["min_duration"]),                # minDuration
                int(args["max_duration"]),                # maxDuration
                args.get("allow_partial_fill", True),     # allowPartialFill
                0,                                       # validFromTimestamp
                expiry,                                  # expiry
                bytes.fromhex(args["market_id"][2:]),     # marketId (bytes32)
                bytes.fromhex(salt[2:]),                  # salt (bytes32)
                int(args.get("grace_period", "86400")),   # gracePeriod
                int(args.get("min_interest_bps", "0")),   # minInterestBps
                [],                                      # conditions
                [],                                      # preHooks
                [],                                      # postHooks
            )

            contract = _w3.eth.contract(abi=LENDING_MATCHER_ABI)
            encoded = contract.encode_abi("registerLendIntent", args=[intent_struct])

            tx_hash = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": encoded}
            )

            return "\n".join([
                "## Lend Intent Posted\n",
                f"- **Approval**: {approval_result or 'Allowance sufficient, no approval needed'}",
                f"- **Transaction**: {tx_hash}",
                f"- **Amount**: {args['amount']} (raw units)",
                f"- **Min Interest Rate**: {format_bps(int(args['min_interest_rate_bps']))}",
                f"- **Max LTV**: {format_bps(int(args['max_ltv_bps']))}",
                f"- **Duration**: {format_duration(int(args['min_duration']))} -- {format_duration(int(args['max_duration']))}",
                f"- **Expiry**: {format_timestamp(expiry)}",
                f"- **Partial Fill**: {'Yes' if args.get('allow_partial_fill', True) else 'No'}",
            ])
        except Exception as e:
            return f"Error posting lend intent: {e}"

    @create_action(
        name="post_borrow_intent",
        description=(
            "Post a borrow intent on Floe. This registers your request to borrow at a fixed "
            "rate and terms. The collateral token is automatically approved before posting."
        ),
        schema=PostBorrowIntentSchema,
    )
    def post_borrow_intent(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            user_address = wallet_provider.get_address()
            now = int(time.time())
            expiry = now + int(args.get("expiry_seconds", "86400"))
            salt = "0x" + secrets.token_hex(32)

            parsed_collateral = int(args["collateral_amount"])

            # Fetch market to resolve collateral token for approval
            market = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getMarket",
                args=[args["market_id"]],
            )

            # Auto-approve collateral token with 1% buffer
            approval_amount = (parsed_collateral * 101) // 100
            approval_result = self._ensure_allowance(
                wallet_provider,
                market.collateralToken,
                self._matcher_address,
                approval_amount,
            )

            on_behalf_of = args.get("on_behalf_of") or user_address
            intent_struct = (
                user_address,                                # borrower
                on_behalf_of,                                # onBehalfOf
                int(args["borrow_amount"]),                  # borrowAmount
                parsed_collateral,                           # collateralAmount
                int(args["min_fill_amount"]),                 # minFillAmount
                int(args["max_interest_rate_bps"]),           # maxInterestRateBps
                int(args["min_ltv_bps"]),                     # minLtvBps
                int(args["min_duration"]),                    # minDuration
                int(args["max_duration"]),                    # maxDuration
                args.get("allow_partial_fill", False),        # allowPartialFill
                0,                                           # validFromTimestamp
                int(args.get("matcher_commission_bps", "50")),  # matcherCommissionBps
                expiry,                                      # expiry
                bytes.fromhex(args["market_id"][2:]),         # marketId (bytes32)
                bytes.fromhex(salt[2:]),                      # salt (bytes32)
                [],                                          # conditions
                [],                                          # preHooks
                [],                                          # postHooks
            )

            contract = _w3.eth.contract(abi=LENDING_MATCHER_ABI)
            encoded = contract.encode_abi("registerBorrowIntent", args=[intent_struct])

            tx_hash = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": encoded}
            )

            return "\n".join([
                "## Borrow Intent Posted\n",
                f"- **Approval**: {approval_result or 'Allowance sufficient, no approval needed'}",
                f"- **Transaction**: {tx_hash}",
                f"- **Borrow Amount**: {args['borrow_amount']} (raw units)",
                f"- **Collateral**: {args['collateral_amount']} (raw units)",
                f"- **Max Interest Rate**: {format_bps(int(args['max_interest_rate_bps']))}",
                f"- **Min LTV**: {format_bps(int(args['min_ltv_bps']))}",
                f"- **Duration**: {format_duration(int(args['min_duration']))} -- {format_duration(int(args['max_duration']))}",
                f"- **Matcher Commission**: {format_bps(int(args.get('matcher_commission_bps', '50')))}",
                f"- **Expiry**: {format_timestamp(expiry)}",
            ] + ([f"- **USDC Sent To**: {format_address(on_behalf_of)}"] if on_behalf_of != user_address else []))
        except Exception as e:
            return f"Error posting borrow intent: {e}"

    @create_action(
        name="match_intents",
        description=(
            "Match a lend intent with a borrow intent to create a loan. Both intents must "
            "be registered on-chain and belong to the same market. This is typically done "
            "by solver bots but can be called by anyone."
        ),
        schema=MatchIntentsSchema,
    )
    def match_intents(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            lend_hash = args["lend_intent_hash"]
            borrow_hash = args["borrow_intent_hash"]
            market_id = args["market_id"]
            zero_address = "0x0000000000000000000000000000000000000000"

            lend_intent = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getOnChainLendIntent",
                args=[lend_hash],
            )
            borrow_intent = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getOnChainBorrowIntent",
                args=[borrow_hash],
            )

            if lend_intent.lender == zero_address:
                return f"Lend intent {lend_hash} not found on-chain."
            if borrow_intent.borrower == zero_address:
                return f"Borrow intent {borrow_hash} not found on-chain."

            # Build the raw tuple representations for encoding
            # Lend intent tuple
            lend_tuple = (
                lend_intent.lender,
                lend_intent.onBehalfOf,
                int(lend_intent.amount),
                int(lend_intent.minFillAmount),
                int(lend_intent.filledAmount),
                int(lend_intent.minInterestRateBps),
                int(lend_intent.maxLtvBps),
                int(lend_intent.minDuration),
                int(lend_intent.maxDuration),
                lend_intent.allowPartialFill,
                int(lend_intent.validFromTimestamp),
                int(lend_intent.expiry),
                lend_intent.marketId,
                lend_intent.salt,
                int(lend_intent.gracePeriod),
                int(lend_intent.minInterestBps),
                list(lend_intent.conditions) if hasattr(lend_intent, 'conditions') else [],
                list(lend_intent.preHooks) if hasattr(lend_intent, 'preHooks') else [],
                list(lend_intent.postHooks) if hasattr(lend_intent, 'postHooks') else [],
            )

            # Borrow intent tuple
            borrow_tuple = (
                borrow_intent.borrower,
                borrow_intent.onBehalfOf,
                int(borrow_intent.borrowAmount),
                int(borrow_intent.collateralAmount),
                int(borrow_intent.minFillAmount),
                int(borrow_intent.maxInterestRateBps),
                int(borrow_intent.minLtvBps),
                int(borrow_intent.minDuration),
                int(borrow_intent.maxDuration),
                borrow_intent.allowPartialFill,
                int(borrow_intent.validFromTimestamp),
                int(borrow_intent.matcherCommissionBps),
                int(borrow_intent.expiry),
                borrow_intent.marketId,
                borrow_intent.salt,
                list(borrow_intent.conditions) if hasattr(borrow_intent, 'conditions') else [],
                list(borrow_intent.preHooks) if hasattr(borrow_intent, 'preHooks') else [],
                list(borrow_intent.postHooks) if hasattr(borrow_intent, 'postHooks') else [],
            )

            contract = _w3.eth.contract(abi=LENDING_MATCHER_ABI)
            encoded = contract.encode_abi("matchLoanIntents",
                args=[
                    lend_tuple,
                    b"",           # lenderSig (empty for on-chain intents)
                    borrow_tuple,
                    b"",           # borrowerSig (empty for on-chain intents)
                    bytes.fromhex(market_id[2:]),
                    True,          # isLenderOnChain
                    True,          # isBorrowerOnChain
                ],
            )

            tx_hash = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": encoded}
            )

            return "\n".join([
                "## Intents Matched\n",
                f"- **Transaction**: {tx_hash}",
                f"- **Lend Intent**: {lend_hash}",
                f"- **Borrow Intent**: {borrow_hash}",
                f"- **Market**: {market_id}",
                "\nA new loan has been created. Check the transaction receipt for the loan ID.",
            ])
        except Exception as e:
            return f"Error matching intents: {e}"

    @create_action(
        name="repay_loan",
        description=(
            "Repay a Floe loan (fully or partially). The loan token is automatically "
            "approved and maxTotalRepayment is calculated with slippage to account for "
            "interest accruing between submission and execution."
        ),
        schema=RepayLoanSchema,
    )
    def repay_loan(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            loan_id = int(args["loan_id"])
            repay_amount = int(args["repay_amount"])
            slippage_bps = int(args.get("slippage_bps", "500"))

            loan = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoan",
                args=[loan_id],
            )
            interest_data = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getAccruedInterest",
                args=[loan_id],
            )
            interest_amount = int(interest_data[0])

            if loan.repaid:
                return f"Loan #{args['loan_id']} is already repaid."

            loan_meta = resolve_token_meta(loan.loanToken, wallet_provider)

            # Calculate proportional interest for partial repayment
            principal = int(loan.principal)
            proportional_interest = (
                (interest_amount * repay_amount) // principal if principal > 0 else 0
            )
            estimated_total = repay_amount + proportional_interest
            max_total_repayment = estimated_total + (estimated_total * slippage_bps) // BASIS_POINTS

            # Auto-approve loan token for repayment
            approval_result = self._ensure_allowance(
                wallet_provider,
                loan.loanToken,
                self._matcher_address,
                max_total_repayment,
            )

            contract = _w3.eth.contract(abi=LENDING_MATCHER_ABI)
            encoded = contract.encode_abi("repayLoan",
                args=[loan_id, repay_amount, max_total_repayment],
            )

            tx_hash = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": encoded}
            )

            return "\n".join([
                "## Loan Repaid\n",
                f"- **Approval**: {approval_result or 'Allowance sufficient, no approval needed'}",
                f"- **Transaction**: {tx_hash}",
                f"- **Loan ID**: {args['loan_id']}",
                f"- **Repay Amount**: {format_token_amount(repay_amount, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Estimated Interest**: {format_token_amount(proportional_interest, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Max Total Repayment (with {format_bps(slippage_bps)} slippage)**: {format_token_amount(max_total_repayment, loan_meta['decimals'], loan_meta['symbol'])}",
            ])
        except Exception as e:
            return f"Error repaying loan: {e}"

    @create_action(
        name="add_collateral",
        description=(
            "Add collateral to an existing Floe loan to improve its health factor and "
            "reduce liquidation risk. The collateral token is automatically approved."
        ),
        schema=AddCollateralSchema,
    )
    def add_collateral(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            loan_id = int(args["loan_id"])
            amount = int(args["amount"])

            loan = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoan",
                args=[loan_id],
            )

            coll_meta = resolve_token_meta(loan.collateralToken, wallet_provider)

            # Auto-approve collateral token (exact amount, no buffer needed)
            approval_result = self._ensure_allowance(
                wallet_provider,
                loan.collateralToken,
                self._matcher_address,
                amount,
            )

            contract = _w3.eth.contract(abi=LENDING_MATCHER_ABI)
            encoded = contract.encode_abi("addCollateral", args=[loan_id, amount])

            tx_hash = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": encoded}
            )

            previous = int(loan.collateralAmount)
            return "\n".join([
                "## Collateral Added\n",
                f"- **Approval**: {approval_result or 'Allowance sufficient, no approval needed'}",
                f"- **Transaction**: {tx_hash}",
                f"- **Loan ID**: {args['loan_id']}",
                f"- **Added**: {format_token_amount(amount, coll_meta['decimals'], coll_meta['symbol'])}",
                f"- **Previous Collateral**: {format_token_amount(previous, coll_meta['decimals'], coll_meta['symbol'])}",
                f"- **New Total**: {format_token_amount(previous + amount, coll_meta['decimals'], coll_meta['symbol'])}",
            ])
        except Exception as e:
            return f"Error adding collateral: {e}"

    @create_action(
        name="withdraw_collateral",
        description=(
            "Withdraw excess collateral from a Floe loan. Only the borrower can call this. "
            "The resulting LTV must stay below the liquidation threshold minus a 3% safety buffer."
        ),
        schema=WithdrawCollateralSchema,
    )
    def withdraw_collateral(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            loan_id = int(args["loan_id"])
            amount = int(args["amount"])

            loan = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoan",
                args=[loan_id],
            )

            coll_meta = resolve_token_meta(loan.collateralToken, wallet_provider)

            contract = _w3.eth.contract(abi=LENDING_MATCHER_ABI)
            encoded = contract.encode_abi("withdrawCollateral", args=[loan_id, amount])

            tx_hash = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": encoded}
            )

            previous = int(loan.collateralAmount)
            return "\n".join([
                "## Collateral Withdrawn\n",
                f"- **Transaction**: {tx_hash}",
                f"- **Loan ID**: {args['loan_id']}",
                f"- **Withdrawn**: {format_token_amount(amount, coll_meta['decimals'], coll_meta['symbol'])}",
                f"- **Previous Collateral**: {format_token_amount(previous, coll_meta['decimals'], coll_meta['symbol'])}",
                f"- **Remaining**: {format_token_amount(previous - amount, coll_meta['decimals'], coll_meta['symbol'])}",
            ])
        except Exception as e:
            return f"Error withdrawing collateral: {e}"

    @create_action(
        name="liquidate_loan",
        description=(
            "Liquidate an unhealthy Floe loan. The loan must have currentLTV >= liquidationLTV "
            "or be overdue. The liquidator pays debt and receives collateral + liquidation "
            "incentive. For underwater loans (collateral < debt), the liquidator gets all "
            "collateral at a discount."
        ),
        schema=LiquidateLoanSchema,
    )
    def liquidate_loan(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            loan_id = int(args["loan_id"])
            repay_amount = int(args["repay_amount"])
            slippage_bps = int(args.get("slippage_bps", "500"))

            loan = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoan",
                args=[loan_id],
            )
            healthy = bool(
                wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="isHealthy",
                    args=[loan_id],
                )
            )
            interest_data = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getAccruedInterest",
                args=[loan_id],
            )
            interest_amount = int(interest_data[0])

            if loan.repaid:
                return f"Loan #{args['loan_id']} is already repaid and cannot be liquidated."

            if healthy:
                return (
                    f"Warning: Loan #{args['loan_id']} is currently healthy. "
                    "Liquidation will revert on-chain. Wait until the loan becomes unhealthy "
                    "(currentLTV >= liquidationLTV or overdue)."
                )

            loan_meta = resolve_token_meta(loan.loanToken, wallet_provider)

            principal = int(loan.principal)
            proportional_interest = (
                (interest_amount * repay_amount) // principal if principal > 0 else 0
            )
            estimated_total = repay_amount + proportional_interest
            max_total_repayment = estimated_total + (estimated_total * slippage_bps) // BASIS_POINTS

            # Auto-approve loan token for liquidation
            approval_result = self._ensure_allowance(
                wallet_provider,
                loan.loanToken,
                self._matcher_address,
                max_total_repayment,
            )

            contract = _w3.eth.contract(abi=LENDING_MATCHER_ABI)
            encoded = contract.encode_abi("liquidateLoan",
                args=[loan_id, repay_amount, max_total_repayment],
            )

            tx_hash = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": encoded}
            )

            return "\n".join([
                "## Loan Liquidated\n",
                f"- **Approval**: {approval_result or 'Allowance sufficient, no approval needed'}",
                f"- **Transaction**: {tx_hash}",
                f"- **Loan ID**: {args['loan_id']}",
                f"- **Repay Amount**: {format_token_amount(repay_amount, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Max Total Repayment (with {format_bps(slippage_bps)} slippage)**: {format_token_amount(max_total_repayment, loan_meta['decimals'], loan_meta['symbol'])}",
                "\nCheck the transaction receipt for the collateral received and final profit.",
            ])
        except Exception as e:
            return f"Error liquidating loan: {e}"

    # ==================================================================
    # FLASH LOAN ACTIONS (16-20)
    # ==================================================================

    @create_action(
        name="get_flash_loan_fee",
        description=(
            "Get Floe's flash loan fee. Flash loans let you borrow any token held by the "
            "protocol within a single transaction -- you must repay principal + fee before "
            "the transaction ends or it reverts atomically."
        ),
        schema=GetFlashLoanFeeSchema,
    )
    def get_flash_loan_fee(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            fee_bps = int(
                wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getFlashloanFeeBps",
                    args=[],
                )
            )

            fee_percent = fee_bps / 100
            example_cost = 1000 * fee_percent / 100

            return "\n".join([
                "## Flash Loan Fee\n",
                f"- **Fee**: {fee_bps} bps ({fee_percent:.2f}%)",
                f"- **Example**: Borrowing 1,000 USDC costs {example_cost:.2f} USDC in fees",
                "\nFlash loans are atomic -- if you can't repay principal + fee in the same "
                "transaction, the entire transaction reverts.",
            ])
        except Exception as e:
            return f"Error fetching flash loan fee: {e}"

    @create_action(
        name="estimate_flash_arb_profit",
        description=(
            "Estimate the profit of a flash loan arbitrage route before executing. Calls "
            "Aerodrome's on-chain QuoterV2 to simulate each swap leg and calculates net "
            "profit after the flash loan fee. Returns an estimate -- actual execution may "
            "differ due to price movement."
        ),
        schema=EstimateFlashArbProfitSchema,
    )
    def estimate_flash_arb_profit(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            token_meta = resolve_token_meta(args["token"], wallet_provider)
            flash_amount = int(args["amount"])

            # Get flash loan fee
            fee_bps = int(
                wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getFlashloanFeeBps",
                    args=[],
                )
            )
            fee_amount = (flash_amount * fee_bps) // BASIS_POINTS

            # Simulate each leg via Aerodrome QuoterV2
            current_amount = flash_amount
            leg_results: list[str] = []

            for i, leg in enumerate(args["legs"]):
                # Handle both dict and Pydantic model access
                token_in = leg["token_in"] if isinstance(leg, dict) else leg.token_in
                token_out = leg["token_out"] if isinstance(leg, dict) else leg.token_out
                tick_spacing = leg["tick_spacing"] if isinstance(leg, dict) else leg.tick_spacing

                in_meta = resolve_token_meta(token_in, wallet_provider)
                out_meta = resolve_token_meta(token_out, wallet_provider)

                try:
                    quote_result = wallet_provider.read_contract(
                        contract_address=AERODROME_QUOTER_V2_ADDRESS,
                        abi=AERODROME_QUOTER_V2_ABI,
                        function_name="quoteExactInputSingle",
                        args=[(
                            token_in,       # tokenIn
                            token_out,      # tokenOut
                            current_amount, # amountIn
                            tick_spacing,   # tickSpacing
                            0,              # sqrtPriceLimitX96
                        )],
                    )

                    amount_out = int(quote_result[0])
                    leg_results.append(
                        f"- **Leg {i + 1}**: "
                        f"{format_token_amount(current_amount, in_meta['decimals'], in_meta['symbol'])} -> "
                        f"{format_token_amount(amount_out, out_meta['decimals'], out_meta['symbol'])}"
                    )
                    current_amount = amount_out
                except Exception as leg_err:
                    leg_results.append(
                        f"- **Leg {i + 1}**: {in_meta['symbol']} -> {out_meta['symbol']} -- "
                        f"**Quote failed** (pool may not exist for tick spacing {tick_spacing})"
                    )
                    return "\n".join([
                        "## Flash Arb Estimate -- Failed\n",
                        *leg_results,
                        f"\nQuote failed at leg {i + 1}. Check that the pool exists and has "
                        f"liquidity for the given tick spacing.",
                    ])

            repayment = flash_amount + fee_amount
            profit_raw = current_amount - repayment if current_amount > repayment else 0
            is_profitable = current_amount > repayment

            return "\n".join([
                "## Flash Arb Profit Estimate\n",
                f"- **Flash Borrow**: {format_token_amount(flash_amount, token_meta['decimals'], token_meta['symbol'])}",
                f"- **Fee**: {format_token_amount(fee_amount, token_meta['decimals'], token_meta['symbol'])} ({format_bps(fee_bps)})",
                f"- **Repayment**: {format_token_amount(repayment, token_meta['decimals'], token_meta['symbol'])}",
                "",
                "### Swap Route",
                *leg_results,
                "",
                f"- **Final Output**: {format_token_amount(current_amount, token_meta['decimals'], token_meta['symbol'])}",
                f"- **Estimated Profit**: {format_token_amount(profit_raw, token_meta['decimals'], token_meta['symbol']) if is_profitable else 'UNPROFITABLE -- output does not cover repayment'}",
                "\n**Disclaimer**: This is an estimate based on current on-chain state. Actual "
                "profit may differ due to price movement, MEV, or gas costs. Gas costs are not "
                "included in this estimate.",
            ])
        except Exception as e:
            return f"Error estimating flash arb profit: {e}"

    @create_action(
        name="get_flash_arb_balance",
        description=(
            "Check the accumulated profit balance in a FlashArbReceiver contract. After "
            "successful arbitrages, profit stays in the receiver contract until the owner "
            "sweeps it via rescueTokens()."
        ),
        schema=GetFlashArbBalanceSchema,
    )
    def get_flash_arb_balance(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            receiver_address = self._resolve_receiver_address(args.get("receiver_address"))
            token_meta = resolve_token_meta(args["token"], wallet_provider)

            balance = int(
                wallet_provider.read_contract(
                    contract_address=args["token"],
                    abi=ERC20_ABI,
                    function_name="balanceOf",
                    args=[receiver_address],
                )
            )

            # Verify it's a FlashArbReceiver by reading the owner
            try:
                owner = str(
                    wallet_provider.read_contract(
                        contract_address=receiver_address,
                        abi=FLASH_ARB_RECEIVER_ABI,
                        function_name="owner",
                        args=[],
                    )
                )
            except Exception:
                return (
                    f"Error: {receiver_address} does not appear to be a FlashArbReceiver "
                    f"contract (owner() call failed)."
                )

            profit_note = (
                "\nProfit is available to sweep. The owner can call rescueTokens() to withdraw."
                if balance > 0
                else "\nNo accumulated profit for this token."
            )

            return "\n".join([
                "## FlashArbReceiver Balance\n",
                f"- **Receiver**: {format_address(receiver_address)}",
                f"- **Owner**: {format_address(owner)}",
                f"- **{token_meta['symbol']} Balance**: {format_token_amount(balance, token_meta['decimals'], token_meta['symbol'])}",
                profit_note,
            ])
        except Exception as e:
            return f"Error checking flash arb balance: {e}"

    @create_action(
        name="flash_loan",
        description=(
            "Execute a raw flash loan from Floe. CRITICAL: The connected wallet (msg.sender) "
            "IS the flash loan receiver -- the protocol sends tokens to msg.sender and calls "
            "receiveFlashLoan() on msg.sender. This means the connected wallet MUST be a "
            "smart contract implementing IFlashloanReceiver. EOA wallets will cause a revert. "
            "For arbitrage with an EOA wallet, use the flash_arb action instead."
        ),
        schema=FlashLoanSchema,
    )
    def flash_loan(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            token_meta = resolve_token_meta(args["token"], wallet_provider)
            amount = int(args["amount"])
            caller_address = wallet_provider.get_address()

            fee_bps = int(
                wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getFlashloanFeeBps",
                    args=[],
                )
            )
            fee_amount = (amount * fee_bps) // BASIS_POINTS

            contract = _w3.eth.contract(abi=LENDING_MATCHER_ABI)
            callback_data = args["callback_data"]
            # Ensure callback_data is bytes
            if isinstance(callback_data, str) and callback_data.startswith("0x"):
                callback_bytes = bytes.fromhex(callback_data[2:])
            else:
                callback_bytes = callback_data if isinstance(callback_data, bytes) else bytes.fromhex(callback_data)

            encoded = contract.encode_abi("flashLoan",
                args=[args["token"], amount, callback_bytes],
            )

            tx_hash = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": encoded}
            )

            return "\n".join([
                "## Flash Loan Submitted\n",
                f"- **Transaction**: {tx_hash}",
                f"- **Token**: {format_token_amount(amount, token_meta['decimals'], token_meta['symbol'])}",
                f"- **Fee**: {format_token_amount(fee_amount, token_meta['decimals'], token_meta['symbol'])} ({format_bps(fee_bps)})",
                f"- **Receiver (msg.sender)**: {format_address(caller_address)}",
                "\nThe protocol will transfer tokens to the caller, invoke receiveFlashLoan(), "
                "then pull repayment. Check the transaction receipt for details.",
            ])
        except Exception as e:
            return f"Error executing flash loan: {e}"

    @create_action(
        name="flash_arb",
        description=(
            "Execute a flash loan arbitrage via a deployed FlashArbReceiver contract. "
            "Borrows tokens from Floe, executes a series of Aerodrome Slipstream swaps, "
            "repays the loan + fee, and retains profit in the receiver contract. The "
            "connected wallet must be the owner of the FlashArbReceiver. Use "
            "estimate_flash_arb_profit first to check profitability."
        ),
        schema=FlashArbSchema,
    )
    def flash_arb(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            receiver_address = self._resolve_receiver_address(args.get("receiver_address"))
            token_meta = resolve_token_meta(args["token"], wallet_provider)
            amount = int(args["amount"])
            min_profit = int(args.get("min_profit", "0"))
            deadline_arg = args.get("deadline")
            deadline = int(deadline_arg) if deadline_arg else int(time.time()) + 300  # 5 minutes

            # Build ArbLeg structs
            legs_raw = args["legs"]
            legs = []
            for leg in legs_raw:
                if isinstance(leg, dict):
                    legs.append({
                        "is_multi_hop": leg.get("is_multi_hop", False),
                        "tick_spacing": leg.get("tick_spacing", 0),
                        "token_in": leg["token_in"],
                        "token_out": leg["token_out"],
                        "amount_in": int(leg.get("amount_in", "0")),
                        "min_amount_out": int(leg.get("min_amount_out", "0")),
                        "path": leg.get("path", "0x"),
                    })
                else:
                    legs.append({
                        "is_multi_hop": leg.is_multi_hop,
                        "tick_spacing": leg.tick_spacing,
                        "token_in": leg.token_in,
                        "token_out": leg.token_out,
                        "amount_in": int(leg.amount_in),
                        "min_amount_out": int(leg.min_amount_out),
                        "path": leg.path or "0x",
                    })

            # ABI-encode ArbParams struct using eth_abi
            # The contract decodes: (ArbLeg[], uint256, uint256)
            # ArbLeg = (bool isMultiHop, int24 tickSpacing, address tokenIn, address tokenOut,
            #           uint256 amountIn, uint256 minAmountOut, bytes path)
            legs_tuples = []
            for leg in legs:
                path_val = leg["path"]
                if isinstance(path_val, str) and path_val.startswith("0x"):
                    path_bytes = bytes.fromhex(path_val[2:]) if len(path_val) > 2 else b""
                else:
                    path_bytes = path_val if isinstance(path_val, bytes) else b""
                legs_tuples.append((
                    leg["is_multi_hop"],
                    leg["tick_spacing"],
                    leg["token_in"],
                    leg["token_out"],
                    leg["amount_in"],
                    leg["min_amount_out"],
                    path_bytes,
                ))

            arb_params_encoded = "0x" + eth_abi_encode(
                ["(bool,int24,address,address,uint256,uint256,bytes)[]", "uint256", "uint256"],
                [legs_tuples, min_profit, deadline],
            ).hex()

            # Call executeArb on the FlashArbReceiver
            receiver_contract = _w3.eth.contract(abi=FLASH_ARB_RECEIVER_ABI)
            encoded = receiver_contract.encode_abi("executeArb",
                args=[
                    args["token"],
                    amount,
                    bytes.fromhex(arb_params_encoded[2:]),
                ],
            )

            tx_hash = wallet_provider.send_transaction(
                transaction={"to": receiver_address, "data": encoded}
            )

            fee_bps = int(
                wallet_provider.read_contract(
                    contract_address=self._matcher_address,
                    abi=LENDING_MATCHER_ABI,
                    function_name="getFlashloanFeeBps",
                    args=[],
                )
            )
            fee_amount = (amount * fee_bps) // BASIS_POINTS

            leg_summary = []
            for i, leg in enumerate(legs):
                in_sym = format_address(leg["token_in"])
                out_sym = format_address(leg["token_out"])
                detail = " (multi-hop)" if leg["is_multi_hop"] else f" (tick {leg['tick_spacing']})"
                leg_summary.append(f"  {i + 1}. {in_sym} -> {out_sym}{detail}")

            deadline_str = datetime.fromtimestamp(deadline, tz=timezone.utc).strftime(
                "%a, %d %b %Y %H:%M:%S GMT"
            )

            return "\n".join([
                "## Flash Arb Executed\n",
                f"- **Transaction**: {tx_hash}",
                f"- **Flash Borrow**: {format_token_amount(amount, token_meta['decimals'], token_meta['symbol'])}",
                f"- **Fee**: {format_token_amount(fee_amount, token_meta['decimals'], token_meta['symbol'])} ({format_bps(fee_bps)})",
                f"- **Min Profit**: {format_token_amount(min_profit, token_meta['decimals'], token_meta['symbol'])}",
                f"- **Receiver**: {format_address(receiver_address)}",
                f"- **Deadline**: {deadline_str}",
                "",
                f"### Route ({len(legs)} legs)",
                *leg_summary,
                "\nProfit remains in the receiver contract. Use get_flash_arb_balance to check, "
                "then rescueTokens() to withdraw.",
            ])
        except Exception as e:
            return f"Error executing flash arb: {e}"

    # ==================================================================
    # DEPLOY / VERIFY / READINESS ACTIONS (21-23)
    # ==================================================================

    @create_action(
        name="deploy_flash_arb_receiver",
        description=(
            "Deploy a new FlashArbReceiver contract. Runs pre-flight checks (flash loan fee, "
            "WETH liquidity, circuit breaker, SwapRouter) and aborts if any blocker is found. "
            "The connected wallet becomes the owner automatically. The deployed address is "
            "stored in session state so subsequent flash_arb / get_flash_arb_balance calls "
            "can use it without an explicit address."
        ),
        schema=DeployFlashArbReceiverSchema,
    )
    def deploy_flash_arb_receiver(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            connected_wallet = wallet_provider.get_address()
            blockers: list[str] = []
            checks: list[str] = []

            # Pre-flight check 1: Flash loan fee
            try:
                fee_bps = int(
                    wallet_provider.read_contract(
                        contract_address=self._matcher_address,
                        abi=LENDING_MATCHER_ABI,
                        function_name="getFlashloanFeeBps",
                        args=[],
                    )
                )
                checks.append(f"- Flash loan fee: {format_bps(fee_bps)} OK")
            except Exception as e:
                blockers.append(f"- Flash loan fee: FAILED to read ({e})")

            # Pre-flight check 2: WETH liquidity in the matcher (pool)
            try:
                weth_balance = int(
                    wallet_provider.read_contract(
                        contract_address=BASE_WETH_ADDRESS,
                        abi=ERC20_ABI,
                        function_name="balanceOf",
                        args=[self._matcher_address],
                    )
                )
                weth_meta = resolve_token_meta(BASE_WETH_ADDRESS, wallet_provider)
                if weth_balance == 0:
                    blockers.append("- WETH liquidity in matcher: 0 -- no flash loans available")
                else:
                    checks.append(
                        f"- WETH liquidity in matcher: "
                        f"{format_token_amount(weth_balance, weth_meta['decimals'], weth_meta['symbol'])} OK"
                    )
            except Exception as e:
                blockers.append(f"- WETH liquidity check: FAILED ({e})")

            # Pre-flight check 3: Circuit breaker
            try:
                is_active = bool(
                    wallet_provider.read_contract(
                        contract_address=BASE_MAINNET_ORACLE,
                        abi=PRICE_ORACLE_ABI,
                        function_name="isCircuitBreakerActive",
                        args=[],
                    )
                )
                if is_active:
                    blockers.append("- Circuit breaker: ACTIVE -- oracle is paused")
                else:
                    checks.append("- Circuit breaker: inactive OK")
            except Exception as e:
                checks.append(f"- Circuit breaker: could not read (non-blocking, {e})")

            # Pre-flight check 4: SwapRouter has code
            try:
                wallet_provider.read_contract(
                    contract_address=AERODROME_SWAP_ROUTER_ADDRESS,
                    abi=_SWAP_ROUTER_FACTORY_ABI,
                    function_name="factory",
                    args=[],
                )
                checks.append(
                    f"- SwapRouter ({format_address(AERODROME_SWAP_ROUTER_ADDRESS)}): has code OK"
                )
            except Exception:
                blockers.append(
                    f"- SwapRouter ({format_address(AERODROME_SWAP_ROUTER_ADDRESS)}): "
                    f"factory() call failed -- may not be deployed"
                )

            if blockers:
                return "\n".join([
                    "## Deploy FlashArbReceiver -- ABORTED\n",
                    "### Blockers",
                    *blockers,
                    "",
                    "### Passed",
                    *checks,
                    "\nFix the blockers above before deploying.",
                ])

            # Deploy using web3.py to encode constructor + bytecode
            deploy_contract = _w3.eth.contract(
                abi=FLASH_ARB_RECEIVER_CONSTRUCTOR_ABI,
                bytecode=FLASH_ARB_RECEIVER_BYTECODE,
            )
            deploy_data = deploy_contract.constructor(
                self._matcher_address,
                AERODROME_SWAP_ROUTER_ADDRESS,
                connected_wallet,
            ).data_in_transaction

            tx_hash = wallet_provider.send_transaction(
                transaction={"to": None, "data": deploy_data}
            )

            # Wait for receipt to get contractAddress
            receipt = wallet_provider.wait_for_transaction_receipt(tx_hash)
            contract_address = None
            if isinstance(receipt, dict):
                contract_address = receipt.get("contractAddress") or receipt.get("contract_address")
            elif hasattr(receipt, "contractAddress"):
                contract_address = receipt.contractAddress
            elif hasattr(receipt, "contract_address"):
                contract_address = receipt.contract_address

            if not contract_address:
                return (
                    f"## Deploy FlashArbReceiver -- FAILED\n\n"
                    f"Transaction {tx_hash} was mined but no contract address in receipt. "
                    f"The deployment may have reverted."
                )

            self._deployed_receiver_address = str(contract_address)

            return "\n".join([
                "## FlashArbReceiver Deployed\n",
                "### Pre-flight checks",
                *checks,
                "",
                "### Deployment",
                f"- **Transaction**: {tx_hash}",
                f"- **Contract Address**: {contract_address}",
                f"- **Owner**: {format_address(connected_wallet)}",
                f"- **Lending Protocol**: {format_address(self._matcher_address)}",
                f"- **Swap Router**: {format_address(AERODROME_SWAP_ROUTER_ADDRESS)}",
                "\nAddress stored in session -- subsequent flash_arb and get_flash_arb_balance "
                "calls will use it automatically.",
            ])
        except Exception as e:
            return f"Error deploying FlashArbReceiver: {e}"

    @create_action(
        name="check_flash_arb_readiness",
        description=(
            "Check whether the environment is ready for flash loan arbitrage. Verifies "
            "flash loan fee, WETH liquidity in the matcher, oracle circuit breaker status, "
            "and SwapRouter availability. If a receiverAddress is provided (or one was "
            "deployed in this session), also validates the receiver's immutables and owner."
        ),
        schema=CheckFlashArbReadinessSchema,
    )
    def check_flash_arb_readiness(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            checks: list[str] = []
            connected_wallet = wallet_provider.get_address()

            # Check 1: Flash loan fee
            try:
                fee_bps = int(
                    wallet_provider.read_contract(
                        contract_address=self._matcher_address,
                        abi=LENDING_MATCHER_ABI,
                        function_name="getFlashloanFeeBps",
                        args=[],
                    )
                )
                checks.append(f"- Flash loan fee: {format_bps(fee_bps)} OK")
            except Exception as e:
                checks.append(f"- Flash loan fee: FAILED ({e})")

            # Check 2: WETH liquidity
            try:
                weth_balance = int(
                    wallet_provider.read_contract(
                        contract_address=BASE_WETH_ADDRESS,
                        abi=ERC20_ABI,
                        function_name="balanceOf",
                        args=[self._matcher_address],
                    )
                )
                weth_meta = resolve_token_meta(BASE_WETH_ADDRESS, wallet_provider)
                if weth_balance == 0:
                    checks.append("- WETH liquidity in matcher: 0 WARNING")
                else:
                    checks.append(
                        f"- WETH liquidity in matcher: "
                        f"{format_token_amount(weth_balance, weth_meta['decimals'], weth_meta['symbol'])} OK"
                    )
            except Exception as e:
                checks.append(f"- WETH liquidity check: FAILED ({e})")

            # Check 3: Circuit breaker
            try:
                is_active = bool(
                    wallet_provider.read_contract(
                        contract_address=BASE_MAINNET_ORACLE,
                        abi=PRICE_ORACLE_ABI,
                        function_name="isCircuitBreakerActive",
                        args=[],
                    )
                )
                if is_active:
                    checks.append("- Circuit breaker: ACTIVE WARNING")
                else:
                    checks.append("- Circuit breaker: inactive OK")
            except Exception as e:
                checks.append(f"- Circuit breaker: could not read ({e})")

            # Check 4: SwapRouter
            try:
                wallet_provider.read_contract(
                    contract_address=AERODROME_SWAP_ROUTER_ADDRESS,
                    abi=_SWAP_ROUTER_FACTORY_ABI,
                    function_name="factory",
                    args=[],
                )
                checks.append(f"- SwapRouter: {format_address(AERODROME_SWAP_ROUTER_ADDRESS)} OK")
            except Exception:
                checks.append(
                    f"- SwapRouter: {format_address(AERODROME_SWAP_ROUTER_ADDRESS)} "
                    f"FAILED -- factory() call failed"
                )

            # Optional receiver checks
            receiver_addr = args.get("receiver_address") or self._deployed_receiver_address
            if receiver_addr:
                checks.append(f"\n### Receiver Verification ({format_address(receiver_addr)})")
                try:
                    owner = str(
                        wallet_provider.read_contract(
                            contract_address=receiver_addr,
                            abi=FLASH_ARB_RECEIVER_ABI,
                            function_name="owner",
                            args=[],
                        )
                    )
                    lending_protocol = str(
                        wallet_provider.read_contract(
                            contract_address=receiver_addr,
                            abi=FLASH_ARB_RECEIVER_ABI,
                            function_name="LENDING_PROTOCOL",
                            args=[],
                        )
                    )
                    swap_router = str(
                        wallet_provider.read_contract(
                            contract_address=receiver_addr,
                            abi=FLASH_ARB_RECEIVER_ABI,
                            function_name="SWAP_ROUTER",
                            args=[],
                        )
                    )

                    owner_match = owner.lower() == connected_wallet.lower()
                    protocol_match = lending_protocol.lower() == self._matcher_address.lower()
                    router_match = swap_router.lower() == AERODROME_SWAP_ROUTER_ADDRESS.lower()

                    checks.append(
                        f"- Owner: {format_address(owner)} "
                        f"{'MATCHES wallet OK' if owner_match else 'MISMATCH -- expected ' + format_address(connected_wallet)}"
                    )
                    checks.append(
                        f"- LENDING_PROTOCOL: {format_address(lending_protocol)} "
                        f"{'OK' if protocol_match else 'MISMATCH -- expected ' + format_address(self._matcher_address)}"
                    )
                    checks.append(
                        f"- SWAP_ROUTER: {format_address(swap_router)} "
                        f"{'OK' if router_match else 'MISMATCH -- expected ' + format_address(AERODROME_SWAP_ROUTER_ADDRESS)}"
                    )
                except Exception as e:
                    checks.append(f"- Receiver read failed: {e}")

            return "\n".join([
                "## Flash Arb Readiness Check\n",
                f"**Wallet**: {format_address(connected_wallet)}",
                f"**Matcher**: {format_address(self._matcher_address)}",
                "",
                "### Environment",
                *checks,
            ])
        except Exception as e:
            return f"Error checking readiness: {e}"

    @create_action(
        name="verify_flash_arb_receiver",
        description=(
            "Verify a deployed FlashArbReceiver contract. Reads owner(), "
            "LENDING_PROTOCOL(), and SWAP_ROUTER() and validates each matches expected "
            "values. Use this to confirm a receiver is correctly configured before "
            "executing arbitrage."
        ),
        schema=VerifyFlashArbReceiverSchema,
    )
    def verify_flash_arb_receiver(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            receiver_address = self._resolve_receiver_address(args.get("receiver_address"))
            connected_wallet = wallet_provider.get_address()
            issues: list[str] = []

            owner = str(
                wallet_provider.read_contract(
                    contract_address=receiver_address,
                    abi=FLASH_ARB_RECEIVER_ABI,
                    function_name="owner",
                    args=[],
                )
            )
            lending_protocol = str(
                wallet_provider.read_contract(
                    contract_address=receiver_address,
                    abi=FLASH_ARB_RECEIVER_ABI,
                    function_name="LENDING_PROTOCOL",
                    args=[],
                )
            )
            swap_router = str(
                wallet_provider.read_contract(
                    contract_address=receiver_address,
                    abi=FLASH_ARB_RECEIVER_ABI,
                    function_name="SWAP_ROUTER",
                    args=[],
                )
            )

            owner_match = owner.lower() == connected_wallet.lower()
            protocol_match = lending_protocol.lower() == self._matcher_address.lower()
            router_match = swap_router.lower() == AERODROME_SWAP_ROUTER_ADDRESS.lower()

            if not owner_match:
                issues.append(
                    f"Owner mismatch: {format_address(owner)} "
                    f"(expected {format_address(connected_wallet)})"
                )
            if not protocol_match:
                issues.append(
                    f"LENDING_PROTOCOL mismatch: {format_address(lending_protocol)} "
                    f"(expected {format_address(self._matcher_address)})"
                )
            if not router_match:
                issues.append(
                    f"SWAP_ROUTER mismatch: {format_address(swap_router)} "
                    f"(expected {format_address(AERODROME_SWAP_ROUTER_ADDRESS)})"
                )

            lines = [
                f"## FlashArbReceiver Verification -- {format_address(receiver_address)}\n",
                f"- **owner()**: {format_address(owner)} {'PASSED' if owner_match else 'FAILED'}",
                f"- **LENDING_PROTOCOL()**: {format_address(lending_protocol)} {'PASSED' if protocol_match else 'FAILED'}",
                f"- **SWAP_ROUTER()**: {format_address(swap_router)} {'PASSED' if router_match else 'FAILED'}",
            ]

            if issues:
                lines.append("\n### ISSUES FOUND")
                for issue in issues:
                    lines.append(f"- {issue}")
            else:
                lines.append(
                    "\nAll checks PASSED. This receiver is correctly configured for use "
                    "with the connected wallet."
                )

            return "\n".join(lines)
        except Exception as e:
            return f"Error verifying FlashArbReceiver: {e}"

    # ════════════════════════════════════════════════════════════════════════
    #  CREDIT FACILITY ACTIONS — at TS parity (7 actions)
    # ════════════════════════════════════════════════════════════════════════
    #
    # High-level credit-facility wrappers for AI agents managing working
    # capital. These 7 actions sit on top of the 23 base Floe actions to
    # reach full parity with agentkit-actions (TypeScript) at 30 Floe
    # actions total. Parity closed in commit 854fd92.
    #
    # Read-only:
    #   - check_credit_status   — combined health/balance/timeline view
    #   - request_credit        — browse available lend offers (RPC event scan)
    #
    # Single-tx:
    #   - repay_credit          — full repayment with auto-slippage
    #
    # Multi-tx flows:
    #   - manual_match_credit   — open a facility against a specific offer
    #                             (2-tx: register + match)
    #   - renew_credit_line     — repay + register + match (3-tx)
    #   - instant_borrow        — auto-select best offer + match (2-tx)
    #   - repay_and_reborrow    — repay + instant_borrow (3-tx)
    #
    # request_credit / instant_borrow / repay_and_reborrow require
    # FloeConfig.rpc_url for the event-log scan path; they raise a clear
    # error at call time if it's missing.
    #
    # Parity tracker: tests/test_action_count.py

    @create_action(
        name="check_credit_status",
        description=(
            "Check the status of an active credit facility (loan). Returns a "
            "combined view of health, remaining balance, accrued interest, and "
            "time to expiry. Designed for AI agents monitoring their working "
            "capital positions."
        ),
        schema=CheckCreditStatusSchema,
    )
    def check_credit_status(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            loan_id = int(args["loan_id"])

            loan = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoan",
                args=[loan_id],
            )
            current_ltv = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getCurrentLtvBps",
                args=[loan_id],
            )
            healthy = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="isHealthy",
                args=[loan_id],
            )
            interest_data = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getAccruedInterest",
                args=[loan_id],
            )

            if loan.repaid:
                return (
                    f"## Credit Facility -- Loan #{args['loan_id']}\n\n"
                    f"**Status**: Fully repaid. No active credit facility."
                )

            loan_meta = resolve_token_meta(loan.loanToken, wallet_provider)
            collateral_meta = resolve_token_meta(loan.collateralToken, wallet_provider)

            interest_amount = int(interest_data[0])
            principal = int(loan.principal)
            rate_bps = int(loan.interestRateBps)
            duration = int(loan.duration)
            min_int_bps = int(loan.minInterestBps)
            start_time = int(loan.startTime)
            grace_period = int(loan.gracePeriod)
            liq_ltv_bps = int(loan.liquidationLtvBps)
            cur_ltv_bps = int(current_ltv)

            end_time = start_time + duration
            grace_end = end_time + grace_period
            now = int(time.time())
            time_remaining = end_time - now if end_time > now else 0
            is_overdue = now > grace_end
            in_grace_period = now > end_time and now <= grace_end

            total_debt = principal + interest_amount
            distance_bps = liq_ltv_bps - cur_ltv_bps

            # Early repayment terms (mirrors TS contract math)
            full_term_interest = (principal * rate_bps * duration) // (10000 * 365 * 24 * 60 * 60)
            is_past_maturity = now >= end_time
            early_repay_penalty = 0
            if not is_past_maturity and min_int_bps > 0:
                min_required = (full_term_interest * min_int_bps) // 10000
                if min_required > interest_amount:
                    early_repay_penalty = min_required - interest_amount
            total_repay_now = principal + interest_amount + early_repay_penalty

            lines = [
                f"## Credit Facility Status -- Loan #{args['loan_id']}\n",
                "### Balance",
                f"- **Principal**: {format_token_amount(principal, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Accrued Interest**: {format_token_amount(interest_amount, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Total Debt**: {format_token_amount(total_debt, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Collateral**: {format_token_amount(int(loan.collateralAmount), collateral_meta['decimals'], collateral_meta['symbol'])}",
                "",
                "### Health",
                f"- **Healthy**: {'Yes' if healthy else 'NO -- Liquidatable!'}",
                f"- **Current LTV**: {format_bps(cur_ltv_bps)}",
                f"- **Liquidation LTV**: {format_bps(liq_ltv_bps)}",
                f"- **Buffer**: {format_bps(distance_bps)} ({compute_health_percent(cur_ltv_bps, liq_ltv_bps)})",
                "",
                "### Timeline",
                f"- **Started**: {format_timestamp(start_time)}",
                f"- **Duration**: {format_duration(duration)}",
                f"- **Time Remaining**: {format_duration(time_remaining) if time_remaining > 0 else ('OVERDUE' if is_overdue else 'Expired (in grace period)')}",
                f"- **Grace Period**: {format_duration(grace_period) if grace_period > 0 else 'Protocol default'}",
                "",
                "### Early Repayment Terms",
                f"- **Min Interest**: {f'{format_bps(min_int_bps)} of full-term interest' if min_int_bps > 0 else 'None (no minimum)'}",
                f"- **Full-Term Interest**: {format_token_amount(full_term_interest, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Early Repay Penalty**: {format_token_amount(early_repay_penalty, loan_meta['decimals'], loan_meta['symbol']) if early_repay_penalty > 0 else 'None'}",
                f"- **Total If Repaid Now**: {format_token_amount(total_repay_now, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Interest Rate**: {format_bps(rate_bps)} APR",
            ]

            if not healthy:
                lines.append(
                    "\n**CRITICAL**: This credit facility can be liquidated. Repay immediately or add collateral."
                )
            elif distance_bps < 500:
                lines.append(
                    "\n**Warning**: Close to liquidation threshold. Consider adding collateral."
                )

            if in_grace_period:
                lines.append(
                    "\n**Warning**: Loan has expired and is in grace period. Repay before grace period ends to avoid overdue liquidation."
                )
            elif is_overdue:
                lines.append(
                    "\n**CRITICAL**: Loan is overdue. It can be liquidated at any time."
                )
            elif 0 < time_remaining < 86400:
                lines.append(
                    "\n**Notice**: Less than 24 hours remaining. Consider repaying or renewing."
                )

            return "\n".join(lines)
        except Exception as e:
            return f"Error checking credit status: {e}"

    @create_action(
        name="repay_credit",
        description=(
            "Fully repay a credit facility (loan). Auto-computes principal + "
            "accrued interest + slippage and submits the repayLoan transaction. "
            "Use repay_loan directly for partial repayment."
        ),
        schema=RepayCreditSchema,
    )
    def repay_credit(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            loan_id = int(args["loan_id"])
            slippage_bps = int(args.get("slippage_bps", "500"))

            loan = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoan",
                args=[loan_id],
            )
            interest_data = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getAccruedInterest",
                args=[loan_id],
            )
            interest_amount = int(interest_data[0])

            if loan.repaid:
                return f"Loan #{args['loan_id']} is already repaid."

            principal = int(loan.principal)
            if principal == 0:
                return f"Loan #{args['loan_id']} has zero principal — already settled."

            loan_meta = resolve_token_meta(loan.loanToken, wallet_provider)

            # Full repayment: repay_amount == principal. Cap includes the
            # contract's early-repayment floor when minInterestBps > 0 and
            # the loan is repaid before maturity — otherwise the cap can
            # be smaller than what repayLoan() demands and the tx reverts.
            estimated_total, max_total_repayment, early_repay_penalty = (
                self._compute_full_repay_total(loan, interest_amount, slippage_bps)
            )

            # Auto-approve loan token for the maximum we might be charged
            approval_result = self._ensure_allowance(
                wallet_provider,
                loan.loanToken,
                self._matcher_address,
                max_total_repayment,
            )

            contract = _w3.eth.contract(abi=LENDING_MATCHER_ABI)
            encoded = contract.encode_abi(
                "repayLoan",
                args=[loan_id, principal, max_total_repayment],
            )

            tx_hash = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": encoded}
            )
            # Wait for the repay to be mined before returning. Callers like
            # repay_and_reborrow otherwise race the next tx against a still-
            # in-flight repay and the reborrow phase can revert intermittently
            # because the old loan isn't yet marked repaid on-chain.
            #
            # If the receipt wait fails or the tx reverted, we must NOT
            # report success — downstream callers (repay_and_reborrow)
            # would then try to open a new facility against a still-open
            # loan. Surface the failure as a clear error instead.
            try:
                receipt = wallet_provider.wait_for_transaction_receipt(tx_hash)
            except Exception as e:
                return (
                    f"Repay transaction submitted ({tx_hash}) but could not be confirmed: {e}. "
                    "Check the transaction receipt before assuming the credit facility is closed."
                )
            receipt_status = None
            if isinstance(receipt, dict):
                receipt_status = receipt.get("status")
            elif hasattr(receipt, "status"):
                receipt_status = getattr(receipt, "status")
            if receipt_status == 0:
                return (
                    f"Repay transaction {tx_hash} reverted on-chain. "
                    f"The credit facility is still open. Check the loan state "
                    f"via check_credit_status and retry with adjusted slippage."
                )

            penalty_line = ""
            if early_repay_penalty > 0:
                penalty_line = (
                    f"\n- **Early Repay Penalty**: "
                    f"{format_token_amount(early_repay_penalty, loan_meta['decimals'], loan_meta['symbol'])}"
                )

            return "\n".join([
                "## Credit Facility Repaid\n",
                f"- **Approval**: {approval_result or 'Allowance sufficient, no approval needed'}",
                f"- **Transaction**: {tx_hash}",
                f"- **Loan ID**: {args['loan_id']}",
                f"- **Principal Repaid**: {format_token_amount(principal, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Estimated Interest**: {format_token_amount(interest_amount, loan_meta['decimals'], loan_meta['symbol'])}"
                + penalty_line,
                f"- **Max Total Repayment (with {format_bps(slippage_bps)} slippage)**: {format_token_amount(max_total_repayment, loan_meta['decimals'], loan_meta['symbol'])}",
                "",
                "Credit facility is now closed. Collateral will be returned to your wallet on the next block.",
            ])
        except Exception as e:
            return f"Error repaying credit facility: {e}"

    # ── Credit-facility helpers ───────────────────────────────────────────

    def _compute_full_repay_total(
        self,
        loan: Any,
        accrued_interest: int,
        slippage_bps: int,
    ) -> tuple[int, int, int]:
        """Compute the estimated total and slippage-adjusted max for a full
        principal repayment, accounting for the contract's min-interest floor.

        Returns (estimated_total, max_total_repayment, early_repay_penalty).

        Mirrors the early-repayment penalty math from check_credit_status:
        if the loan has a nonzero minInterestBps and is repaid BEFORE
        maturity, the contract requires at least
            full_term_interest * minInterestBps / BASIS_POINTS
        of interest, even if the actually-accrued interest is lower. Failing
        to include this in the slippage cushion makes repayLoan() revert on
        any loan with a non-trivial minInterestBps when repaid early.
        """
        principal = int(loan.principal)
        rate_bps = int(loan.interestRateBps)
        duration = int(loan.duration)
        start_time = int(loan.startTime)
        min_int_bps = int(getattr(loan, "minInterestBps", 0))

        # Full-term interest at the loan's fixed rate (simple interest
        # per second, matching the contract's accrual formula).
        full_term_interest = (
            principal * rate_bps * duration
        ) // (BASIS_POINTS * 365 * 24 * 60 * 60)

        early_repay_penalty = 0
        now = int(time.time())
        is_past_maturity = now >= start_time + duration
        if not is_past_maturity and min_int_bps > 0:
            min_required = (full_term_interest * min_int_bps) // BASIS_POINTS
            if min_required > accrued_interest:
                early_repay_penalty = min_required - accrued_interest

        estimated_total = principal + accrued_interest + early_repay_penalty
        max_total_repayment = (
            estimated_total + (estimated_total * slippage_bps) // BASIS_POINTS
        )
        return estimated_total, max_total_repayment, early_repay_penalty

    def _get_public_client(self) -> Web3:
        """Lazy Web3 HTTP client for event log scanning. Requires rpc_url."""
        if self._public_client is None:
            if not self._rpc_url:
                raise ValueError(
                    "RPC URL not configured. Pass rpc_url in FloeConfig to "
                    "browse available credit offers (request_credit, "
                    "instant_borrow, repay_and_reborrow)."
                )
            self._public_client = Web3(Web3.HTTPProvider(self._rpc_url))
        return self._public_client

    def _scan_available_lend_intents(
        self,
        wallet_provider: EvmWalletProvider,
        market_id: str,
    ) -> list[dict]:
        """Scan LogLenderOfferPosted events for a market and read intents.

        Returns list of {hash, intent} for offers that are unrevoked, partially
        unfilled, and unexpired. Intended to be monkey-patched in tests.

        Scan window: by default, looks back `FloeConfig.offer_scan_lookback_blocks`
        blocks from head (default ~7 days on Base). This keeps the query
        bounded so it stays inside Alchemy/Infura eth_getLogs range limits
        and response times don't grow with protocol usage. Set the config
        value to None to scan from matcher deployment block (legacy behavior
        — will hit RPC limits as history grows).
        """
        client = self._get_public_client()
        contract = client.eth.contract(
            address=Web3.to_checksum_address(self._matcher_address),
            abi=LOG_LENDER_OFFER_POSTED_EVENT,
        )
        market_id_bytes = bytes.fromhex(market_id[2:])

        if self._offer_scan_lookback_blocks is None:
            from_block: int = MATCHER_DEPLOYMENT_BLOCK
        else:
            latest = client.eth.block_number
            from_block = max(
                MATCHER_DEPLOYMENT_BLOCK,
                latest - self._offer_scan_lookback_blocks,
            )

        logs = contract.events.LogLenderOfferPosted.get_logs(
            from_block=from_block,
            to_block="latest",
            argument_filters={"marketId": market_id_bytes},
        )
        seen: set[bytes] = set()
        unique_hashes: list[bytes] = []
        for log in logs:
            h = log["args"]["offerHash"]
            if h not in seen:
                seen.add(h)
                unique_hashes.append(h)

        now = int(time.time())
        zero_addr = "0x0000000000000000000000000000000000000000"
        results: list[dict] = []
        for h in unique_hashes:
            intent = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getOnChainLendIntent",
                args=[h],
            )
            if (
                getattr(intent, "lender", zero_addr).lower() != zero_addr
                and int(intent.filledAmount) < int(intent.amount)
                and int(intent.expiry) > now
                # Filter out offers whose valid-from window hasn't opened
                # yet. Advertising them would let request_credit surface
                # an intent that instant_borrow then can't actually match.
                and int(getattr(intent, "validFromTimestamp", 0)) <= now
            ):
                results.append({"hash": "0x" + h.hex(), "intent": intent})
        return results

    def _check_lend_intent_compatibility(
        self,
        lend_intent: Any,
        *,
        market_id: str,
        borrow_amount: int,
        max_rate_bps: int,
        min_ltv_bps: int,
        duration: int,
    ) -> Optional[str]:
        """Preflight a borrow request against a lend intent.

        Returns a human-readable error string if the intent is incompatible
        (wrong market, revoked, expired, not yet valid, rate/duration/LTV
        mismatch, insufficient remaining), or None if the intent is
        matchable. Used by both manual_match_credit (single-intent path)
        and instant_borrow (auto-select path) so both routes apply the
        same rules and fail BEFORE sending registerBorrowIntent — which
        would otherwise leave stray on-chain state when matchLoanIntents
        reverts.
        """
        zero_addr = "0x0000000000000000000000000000000000000000"
        if getattr(lend_intent, "lender", zero_addr).lower() == zero_addr:
            return "Lend intent not found on-chain. It may have been revoked or already fully matched."

        # Market must match — matching across markets would revert anyway.
        intent_market_raw = getattr(lend_intent, "marketId", None)
        if isinstance(intent_market_raw, (bytes, bytearray)):
            intent_market = "0x" + intent_market_raw.hex()
        else:
            intent_market = str(intent_market_raw) if intent_market_raw is not None else ""
        if intent_market.lower() != market_id.lower():
            return (
                f"Lend intent belongs to market {intent_market}, but borrow requested market {market_id}."
            )

        now = int(time.time())
        if int(getattr(lend_intent, "validFromTimestamp", 0)) > now:
            return "Lend intent is not yet valid (validFromTimestamp in the future)."
        if int(lend_intent.expiry) <= now:
            return "Lend intent has expired."

        remaining = int(lend_intent.amount) - int(lend_intent.filledAmount)
        if remaining < borrow_amount:
            return (
                f"Lend intent only has {remaining} remaining (raw units), "
                f"but you requested {borrow_amount}."
            )

        # Exact-fill constraint: _build_borrow_struct always posts
        # min_fill_amount == borrow_amount with allow_partial_fill=False,
        # so a lend intent that disallows partial fills can only accept a
        # borrow EQUAL to its remaining amount. A lend intent with
        # minFillAmount > borrow_amount also can't accept this request.
        # Without these checks, the borrow intent registers on-chain
        # (TX1) and matchLoanIntents reverts on TX2, wasting gas.
        allow_partial = bool(getattr(lend_intent, "allowPartialFill", True))
        if not allow_partial and borrow_amount != remaining:
            return (
                f"Lend intent does not allow partial fills; requested {borrow_amount} "
                f"but remaining amount is {remaining} (must match exactly)."
            )
        min_fill = int(getattr(lend_intent, "minFillAmount", 0))
        if borrow_amount < min_fill:
            return (
                f"Requested borrow_amount ({borrow_amount}) is below the lend intent's "
                f"minimum fill ({min_fill})."
            )

        if int(lend_intent.minInterestRateBps) > max_rate_bps:
            return (
                f"Requested max_interest_rate_bps ({max_rate_bps}) is below the "
                f"lend intent's minimum rate ({int(lend_intent.minInterestRateBps)})."
            )
        if int(lend_intent.maxLtvBps) < min_ltv_bps:
            return (
                f"Requested min_ltv_bps ({min_ltv_bps}) exceeds the lend intent's "
                f"max LTV ({int(lend_intent.maxLtvBps)})."
            )
        if duration < int(lend_intent.minDuration) or duration > int(lend_intent.maxDuration):
            return (
                f"Requested duration ({duration}s) is outside the lend intent's "
                f"allowed range [{int(lend_intent.minDuration)}, {int(lend_intent.maxDuration)}]s."
            )
        return None

    def _extract_loan_id_from_receipt(self, receipt: Any) -> Optional[str]:
        """Parse LogIntentsMatchedDetailed from a transaction receipt's logs."""
        try:
            logs = receipt.get("logs") if isinstance(receipt, dict) else getattr(receipt, "logs", None)
            if not logs:
                return None
            contract = _w3.eth.contract(abi=LOG_INTENTS_MATCHED_DETAILED_EVENT)
            event = contract.events.LogIntentsMatchedDetailed()
            for log in logs:
                try:
                    decoded = event.process_log(log)
                    return str(decoded["args"]["loanId"])
                except Exception:
                    continue
        except Exception:
            return None
        return None

    def _random_salt(self) -> str:
        return "0x" + secrets.token_hex(32)

    def _lend_intent_to_tuple(self, lend_intent: Any) -> tuple:
        """Convert a getOnChainLendIntent result into the ABI tuple ordering.

        Preserves `conditions`, `preHooks`, and `postHooks` from the on-chain
        struct — hardcoding them to empty arrays would produce a different
        tuple than what was originally posted, causing `matchLoanIntents` to
        revert on any intent that carries hooks (the matcher verifies the
        tuple hash against the stored one). Mirrors the pattern used in
        `match_intents` a few hundred lines above.
        """
        market_id = lend_intent.marketId
        if isinstance(market_id, str) and market_id.startswith("0x"):
            market_id = bytes.fromhex(market_id[2:])
        salt = lend_intent.salt
        if isinstance(salt, str) and salt.startswith("0x"):
            salt = bytes.fromhex(salt[2:])
        return (
            Web3.to_checksum_address(lend_intent.lender),
            Web3.to_checksum_address(lend_intent.onBehalfOf),
            int(lend_intent.amount),
            int(lend_intent.minFillAmount),
            int(lend_intent.filledAmount),
            int(lend_intent.minInterestRateBps),
            int(lend_intent.maxLtvBps),
            int(lend_intent.minDuration),
            int(lend_intent.maxDuration),
            bool(lend_intent.allowPartialFill),
            int(lend_intent.validFromTimestamp),
            int(lend_intent.expiry),
            market_id,
            salt,
            int(lend_intent.gracePeriod),
            int(lend_intent.minInterestBps),
            list(lend_intent.conditions) if hasattr(lend_intent, 'conditions') else [],
            list(lend_intent.preHooks) if hasattr(lend_intent, 'preHooks') else [],
            list(lend_intent.postHooks) if hasattr(lend_intent, 'postHooks') else [],
        )

    def _build_borrow_struct(
        self,
        *,
        borrower: str,
        on_behalf_of: str,
        borrow_amount: int,
        collateral_amount: int,
        max_rate_bps: int,
        min_ltv_bps: int,
        duration: int,
        matcher_commission_bps: int,
        expiry: int,
        market_id: str,
        salt: str,
    ) -> tuple:
        """Tuple form of BorrowIntent matching the Solidity ABI ordering."""
        market_id_b = bytes.fromhex(market_id[2:]) if isinstance(market_id, str) else market_id
        salt_b = bytes.fromhex(salt[2:]) if isinstance(salt, str) else salt
        return (
            Web3.to_checksum_address(borrower),
            Web3.to_checksum_address(on_behalf_of),
            borrow_amount,
            collateral_amount,
            borrow_amount,  # min_fill_amount
            max_rate_bps,
            min_ltv_bps,
            duration,
            duration,
            False,  # allow_partial_fill
            0,  # valid_from_timestamp
            matcher_commission_bps,
            expiry,
            market_id_b,
            salt_b,
            [],  # conditions
            [],  # pre_hooks
            [],  # post_hooks
        )

    @create_action(
        name="request_credit",
        description=(
            "Browse available credit offers for a market. Scans on-chain events "
            "and reads intent data directly from the contract. Shows how much "
            "capital is available, at what rates, and for how long. Use this to "
            "find a lend intent to match against with manual_match_credit. "
            "Requires rpc_url in FloeConfig."
        ),
        schema=RequestCreditSchema,
    )
    def request_credit(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            market_id = args["market_id"]
            available = self._scan_available_lend_intents(wallet_provider, market_id)
            market = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getMarket",
                args=[market_id],
            )

            if not available:
                return (
                    f"## No Credit Offers Available\n\n"
                    f"No open lend intents found for market {market_id}. "
                    f"Try a different market or check back later."
                )

            loan_meta = resolve_token_meta(market.loanToken, wallet_provider)
            collateral_meta = resolve_token_meta(market.collateralToken, wallet_provider)

            filtered = available
            if args.get("min_amount"):
                min_amt = int(args["min_amount"])
                filtered = [
                    o for o in filtered
                    if int(o["intent"].amount) - int(o["intent"].filledAmount) >= min_amt
                ]
            if args.get("max_rate_bps"):
                max_rate = int(args["max_rate_bps"])
                filtered = [
                    o for o in filtered
                    if int(o["intent"].minInterestRateBps) <= max_rate
                ]

            filtered.sort(
                key=lambda o: int(o["intent"].amount) - int(o["intent"].filledAmount),
                reverse=True,
            )
            max_results = int(args.get("max_results", 10))
            filtered = filtered[:max_results]

            if not filtered:
                return (
                    f"## No Matching Credit Offers\n\n"
                    f"Found {len(available)} open offer(s) in "
                    f"{loan_meta['symbol']}/{collateral_meta['symbol']}, but none match your filters."
                )

            lines = [
                f"## Available Credit Offers -- {loan_meta['symbol']}/{collateral_meta['symbol']}\n",
                f"Found {len(filtered)} offer(s):\n",
            ]
            for entry in filtered:
                h = entry["hash"]
                intent = entry["intent"]
                remaining = int(intent.amount) - int(intent.filledAmount)
                grace = int(getattr(intent, "gracePeriod", 0))
                lines.extend([
                    f"### Offer `{h[:10]}...`",
                    f"- **Offer Hash**: {h}",
                    f"- **Lender**: {format_address(intent.lender)}",
                    f"- **Available**: {format_token_amount(remaining, loan_meta['decimals'], loan_meta['symbol'])}",
                    f"- **Min Interest Rate**: {format_bps(int(intent.minInterestRateBps))}",
                    f"- **Max LTV**: {format_bps(int(intent.maxLtvBps))}",
                    f"- **Duration**: {format_duration(int(intent.minDuration))} -- {format_duration(int(intent.maxDuration))}",
                    f"- **Expiry**: {format_timestamp(int(intent.expiry))}",
                    f"- **Partial Fill**: {'Yes' if intent.allowPartialFill else 'No'}",
                    f"- **Grace Period**: {format_duration(grace) if grace > 0 else 'Protocol default'}",
                    "",
                ])
            lines.append(
                "\nTo open a credit facility, use **manual_match_credit** with the "
                "offer hash of your chosen offer."
            )
            return "\n".join(lines)
        except Exception as e:
            return f"Error browsing credit offers: {e}"

    @create_action(
        name="manual_match_credit",
        description=(
            "Open a credit facility by matching against a specific lend intent. "
            "Two-transaction operation: (1) registers your borrow intent with "
            "automatic collateral approval, (2) matches it with the chosen lend "
            "intent to create a loan. Returns the new loan ID on success."
        ),
        schema=ManualMatchCreditSchema,
    )
    def manual_match_credit(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            user = wallet_provider.get_address()
            lend_hash = args["lend_intent_hash"]
            market_id = args["market_id"]
            borrow_amount = int(args["borrow_amount"])
            collateral_amount = int(args["collateral_amount"])
            max_rate_bps = int(args["max_interest_rate_bps"])
            min_ltv_bps = int(args["min_ltv_bps"])
            duration = int(args["duration"])
            matcher_commission_bps = int(args.get("matcher_commission_bps", "50"))
            expiry_seconds = int(args.get("expiry_seconds", "300"))
            # Optional proceeds recipient — mirrors post_borrow_intent. If
            # omitted, borrowed USDC lands in the caller's own wallet.
            on_behalf_of = args.get("on_behalf_of") or user
            now = int(time.time())
            expiry = now + expiry_seconds
            salt = self._random_salt()

            market = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getMarket",
                args=[market_id],
            )
            lend_intent = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getOnChainLendIntent",
                args=[lend_hash],
            )

            # Preflight ALL compatibility checks BEFORE sending TX1. If TX1
            # (registerBorrowIntent) lands but TX2 (matchLoanIntents) reverts
            # because of a market/rate/duration/LTV/validFrom mismatch, we
            # leave stray on-chain state and waste the user's gas on TX1.
            incompat = self._check_lend_intent_compatibility(
                lend_intent,
                market_id=market_id,
                borrow_amount=borrow_amount,
                max_rate_bps=max_rate_bps,
                min_ltv_bps=min_ltv_bps,
                duration=duration,
            )
            if incompat is not None:
                return f"Cannot open credit facility against {lend_hash}: {incompat}"

            # Auto-approve 101% of collateral
            approval_amount = (collateral_amount * 101) // 100
            approval_result = self._ensure_allowance(
                wallet_provider,
                market.collateralToken,
                self._matcher_address,
                approval_amount,
            )

            borrow_struct = self._build_borrow_struct(
                borrower=user,
                on_behalf_of=on_behalf_of,
                borrow_amount=borrow_amount,
                collateral_amount=collateral_amount,
                max_rate_bps=max_rate_bps,
                min_ltv_bps=min_ltv_bps,
                duration=duration,
                matcher_commission_bps=matcher_commission_bps,
                expiry=expiry,
                market_id=market_id,
                salt=salt,
            )

            contract = _w3.eth.contract(abi=LENDING_MATCHER_ABI)

            # TX 1: register borrow intent
            register_data = contract.encode_abi(
                "registerBorrowIntent", args=[borrow_struct]
            )
            register_tx = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": register_data}
            )
            wallet_provider.wait_for_transaction_receipt(register_tx)

            # TX 2: match
            lend_tuple = self._lend_intent_to_tuple(lend_intent)
            market_id_bytes = bytes.fromhex(market_id[2:])
            match_data = contract.encode_abi(
                "matchLoanIntents",
                args=[lend_tuple, b"", borrow_struct, b"", market_id_bytes, True, True],
            )
            match_tx = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": match_data}
            )
            match_receipt = wallet_provider.wait_for_transaction_receipt(match_tx)

            loan_id = self._extract_loan_id_from_receipt(match_receipt)
            loan_meta = resolve_token_meta(market.loanToken, wallet_provider)
            collateral_meta = resolve_token_meta(market.collateralToken, wallet_provider)

            return "\n".join([
                "## Credit Facility Opened\n",
                f"- **Approval**: {approval_result or 'Allowance sufficient, no approval needed'}",
                f"- **Register Borrow Intent TX**: {register_tx}",
                f"- **Match TX**: {match_tx}",
                f"- **Loan ID**: {loan_id or 'Check transaction receipt'}",
                f"- **Borrowed**: {format_token_amount(borrow_amount, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Collateral**: {format_token_amount(collateral_amount, collateral_meta['decimals'], collateral_meta['symbol'])}",
                f"- **Interest Rate**: up to {format_bps(max_rate_bps)}",
                f"- **Duration**: {format_duration(duration)}",
            ])
        except Exception as e:
            return f"Error opening credit facility: {e}"

    @create_action(
        name="renew_credit_line",
        description=(
            "Renew an expiring credit facility in two phases: repay the existing "
            "loan, then open a new one by matching a fresh lend intent. Executes "
            "3 transactions: (1) repay existing loan, (2) register new borrow "
            "intent, (3) match with new lend intent. If the second phase fails, "
            "the repayment still succeeds and is reported."
        ),
        schema=RenewCreditLineSchema,
    )
    def renew_credit_line(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            old_loan_id = int(args["loan_id"])
            slippage_bps = int(args.get("slippage_bps", "500"))

            # ── Phase 1: repay existing loan ──
            old_loan = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoan",
                args=[old_loan_id],
            )
            interest_data = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getAccruedInterest",
                args=[old_loan_id],
            )
            if old_loan.repaid:
                return (
                    f"Loan #{args['loan_id']} is already repaid. Use "
                    f"manual_match_credit to open a new credit facility."
                )

            loan_meta = resolve_token_meta(old_loan.loanToken, wallet_provider)
            principal = int(old_loan.principal)
            interest_amount = int(interest_data[0])
            # Use the shared full-repay helper so the minInterestBps
            # early-repayment penalty is included in the slippage cap.
            # Same rationale as in repay_credit — without this, loans
            # with nonzero minInterestBps repaid before maturity would
            # revert on the cap being smaller than the contract's floor.
            _, max_total_repayment, _ = self._compute_full_repay_total(
                old_loan, interest_amount, slippage_bps
            )
            repay_approval = self._ensure_allowance(
                wallet_provider,
                old_loan.loanToken,
                self._matcher_address,
                max_total_repayment,
            )
            contract = _w3.eth.contract(abi=LENDING_MATCHER_ABI)
            repay_data = contract.encode_abi(
                "repayLoan", args=[old_loan_id, principal, max_total_repayment]
            )
            repay_tx = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": repay_data}
            )
            # Fail-hard on a reverted or unconfirmable repay — do NOT
            # proceed to open a new credit line against a still-open loan.
            try:
                repay_receipt = wallet_provider.wait_for_transaction_receipt(repay_tx)
            except Exception as e:
                return (
                    f"Repay transaction submitted ({repay_tx}) but could not be confirmed: {e}. "
                    "New credit facility was NOT opened. Check the repay receipt before retrying."
                )
            repay_status = None
            if isinstance(repay_receipt, dict):
                repay_status = repay_receipt.get("status")
            elif hasattr(repay_receipt, "status"):
                repay_status = getattr(repay_receipt, "status")
            if repay_status == 0:
                return (
                    f"Repay transaction {repay_tx} reverted on-chain. "
                    f"The old credit facility is still open and no new facility was opened. "
                    f"Retry with adjusted slippage."
                )

            # ── Phase 2: open new credit facility ──
            try:
                new_result = self.manual_match_credit(
                    wallet_provider,
                    {
                        "lend_intent_hash": args["lend_intent_hash"],
                        "borrow_amount": args["borrow_amount"],
                        "collateral_amount": args["collateral_amount"],
                        "max_interest_rate_bps": args["max_interest_rate_bps"],
                        "min_ltv_bps": args["min_ltv_bps"],
                        "duration": args["duration"],
                        "market_id": args["market_id"],
                        "matcher_commission_bps": args.get("matcher_commission_bps", "50"),
                        "expiry_seconds": "300",
                    },
                )
                # Check for SUCCESS marker rather than blacklisting known
                # failure prefixes. manual_match_credit can return several
                # non-success strings that don't start with "Error"
                # (e.g. "Lend intent only has X remaining" or "not found
                # on-chain"); the success path always begins with
                # "## Credit Facility Opened".
                if not new_result.startswith("## Credit Facility Opened"):
                    raise RuntimeError(new_result)
            except Exception as match_err:
                return "\n".join([
                    "## Credit Line -- Partial Renewal\n",
                    "### Old Loan Repaid",
                    f"- **Repay TX**: {repay_tx}",
                    f"- **Loan ID**: {args['loan_id']}",
                    f"- **Principal Repaid**: {format_token_amount(principal, loan_meta['decimals'], loan_meta['symbol'])}",
                    f"- **Repay Approval**: {repay_approval or 'No approval needed'}",
                    "",
                    "### New Credit -- FAILED",
                    f"{match_err}",
                    "\nThe old loan was repaid successfully. Use **manual_match_credit** to retry.",
                ])

            return "\n".join([
                "## Credit Line Renewed\n",
                "### Old Loan Repaid",
                f"- **Repay TX**: {repay_tx}",
                f"- **Old Loan ID**: {args['loan_id']}",
                f"- **Principal Repaid**: {format_token_amount(principal, loan_meta['decimals'], loan_meta['symbol'])}",
                f"- **Repay Approval**: {repay_approval or 'No approval needed'}",
                "",
                new_result,
            ])
        except Exception as e:
            return f"Error renewing credit line: {e}"

    @create_action(
        name="instant_borrow",
        description=(
            "Instantly borrow funds by auto-selecting the best available lend "
            "intent. Single action: queries the on-chain intent book, picks the "
            "lowest-rate compatible offer, and executes the 2-tx borrow flow. "
            "For DeFi agents that need capital in seconds, not minutes of "
            "browsing. Requires rpc_url in FloeConfig."
        ),
        schema=InstantBorrowSchema,
    )
    def instant_borrow(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            market_id = args["market_id"]
            borrow_amount = int(args["borrow_amount"])
            max_rate_bps = int(args["max_interest_rate_bps"])
            min_ltv_bps = int(args.get("min_ltv_bps", "8000"))
            duration = int(args["duration"])

            available = self._scan_available_lend_intents(wallet_provider, market_id)
            # Apply the same preflight rules that manual_match_credit will
            # re-check before TX1. Using the shared helper keeps both paths
            # in sync — otherwise instant_borrow could auto-select an offer
            # (e.g. one with maxLtvBps < min_ltv_bps or a future
            # validFromTimestamp) that manual_match_credit would then reject
            # downstream, wasting the whole round-trip.
            compatible = [
                entry
                for entry in available
                if self._check_lend_intent_compatibility(
                    entry["intent"],
                    market_id=market_id,
                    borrow_amount=borrow_amount,
                    max_rate_bps=max_rate_bps,
                    min_ltv_bps=min_ltv_bps,
                    duration=duration,
                )
                is None
            ]

            if not compatible:
                return (
                    "No matching liquidity found for your borrow request. "
                    "Try adjusting your max_interest_rate_bps, duration, or "
                    "borrow_amount."
                )

            # Pick lowest rate
            compatible.sort(key=lambda e: int(e["intent"].minInterestRateBps))
            best = compatible[0]

            match_args: dict[str, Any] = {
                "lend_intent_hash": best["hash"],
                "borrow_amount": args["borrow_amount"],
                "collateral_amount": args["collateral_amount"],
                "max_interest_rate_bps": args["max_interest_rate_bps"],
                "min_ltv_bps": args.get("min_ltv_bps", "8000"),
                "duration": args["duration"],
                "market_id": market_id,
                "matcher_commission_bps": "50",
                "expiry_seconds": "300",
            }
            # Thread on_behalf_of through when the caller supplied one — the
            # schema advertises it and silently ignoring it would be a
            # misleading API contract.
            if args.get("on_behalf_of") is not None:
                match_args["on_behalf_of"] = args["on_behalf_of"]
            return self.manual_match_credit(wallet_provider, match_args)
        except Exception as e:
            return f"Error in instant borrow: {e}"

    @create_action(
        name="repay_and_reborrow",
        description=(
            "Repay an existing credit facility and instantly borrow again in "
            "one action. Auto-selects the best available lend intent for the "
            "new loan. If the reborrow fails (no liquidity), the repayment "
            "still succeeds. Use this for agents cycling credit continuously."
        ),
        schema=RepayAndReborrowSchema,
    )
    def repay_and_reborrow(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            old_loan_id = int(args["loan_id"])
            old_loan = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=LENDING_MATCHER_ABI,
                function_name="getLoan",
                args=[old_loan_id],
            )
            if old_loan.repaid:
                return (
                    f"Loan #{args['loan_id']} is already repaid. Use "
                    f"instant_borrow to open a new credit facility."
                )

            # Step 1: repay
            repay_result = self.repay_credit(
                wallet_provider,
                {
                    "loan_id": args["loan_id"],
                    "slippage_bps": args.get("slippage_bps", "500"),
                },
            )
            if repay_result.startswith("Error"):
                return repay_result

            # Step 2: instant_borrow with old loan defaults if not provided.
            # Every defaultable parameter — including min_ltv_bps — is
            # copied from the old loan so a renewal doesn't silently
            # tighten constraints and break a facility that was opened
            # under looser terms.
            borrow_amount = args.get("new_borrow_amount") or str(int(old_loan.principal))
            collateral_amount = (
                args.get("new_collateral_amount") or str(int(old_loan.collateralAmount))
            )
            max_rate = (
                args.get("max_interest_rate_bps") or str(int(old_loan.interestRateBps))
            )
            duration = args.get("duration") or str(int(old_loan.duration))
            # Reuse the old loan's origination LTV on renewal. Hardcoding
            # 8000 would reject any facility that was opened below 80%.
            min_ltv_bps = str(int(old_loan.ltvBps))
            market_id = "0x" + old_loan.marketId.hex() if isinstance(old_loan.marketId, (bytes, bytearray)) else old_loan.marketId

            borrow_args: dict[str, Any] = {
                "market_id": market_id,
                "borrow_amount": borrow_amount,
                "collateral_amount": collateral_amount,
                "max_interest_rate_bps": max_rate,
                "duration": duration,
                "min_ltv_bps": min_ltv_bps,
            }
            # Thread on_behalf_of through — RepayAndReborrowSchema advertises
            # it and silently ignoring it would be misleading.
            if args.get("on_behalf_of") is not None:
                borrow_args["on_behalf_of"] = args["on_behalf_of"]
            new_result = self.instant_borrow(wallet_provider, borrow_args)

            # Check for SUCCESS marker rather than blacklisting known failure
            # prefixes. instant_borrow surfaces several non-success strings
            # ("No matching liquidity...", "Error ...", errors bubbled up from
            # manual_match_credit like "Lend intent only has X remaining").
            # Success always begins with "## Credit Facility Opened".
            if not new_result.startswith("## Credit Facility Opened"):
                return "\n".join([
                    "## Credit Line -- Partial Renewal\n",
                    "### Old Loan Repaid",
                    repay_result,
                    "",
                    "### New Credit -- FAILED",
                    new_result,
                    "\nOld loan repaid successfully. Use **instant_borrow** to retry.",
                ])

            return "\n".join([
                "## Credit Line Renewed\n",
                "### Old Loan Repaid",
                repay_result,
                "",
                "### New Credit Facility Opened",
                new_result,
            ])
        except Exception as e:
            return f"Error in repay and reborrow: {e}"
