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
    ORACLE_PRICE_SCALE,
    PRICE_ORACLE_ABI,
)
from .flash_arb_bytecode import (
    FLASH_ARB_RECEIVER_BYTECODE,
    FLASH_ARB_RECEIVER_CONSTRUCTOR_ABI,
)
from .schemas import (
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
        self._deployed_receiver_address: Optional[str] = None

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
        encoded = contract.encode_abi(fn_name="approve", args=[spender_address, required_amount])

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
            encoded = contract.encode_abi(fn_name="registerLendIntent", args=[intent_struct])

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
            encoded = contract.encode_abi(fn_name="registerBorrowIntent", args=[intent_struct])

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
            encoded = contract.encode_abi(
                fn_name="matchLoanIntents",
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
            encoded = contract.encode_abi(
                fn_name="repayLoan",
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
            encoded = contract.encode_abi(fn_name="addCollateral", args=[loan_id, amount])

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
            encoded = contract.encode_abi(fn_name="withdrawCollateral", args=[loan_id, amount])

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
            encoded = contract.encode_abi(
                fn_name="liquidateLoan",
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

            encoded = contract.encode_abi(
                fn_name="flashLoan",
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
            encoded = receiver_contract.encode_abi(
                fn_name="executeArb",
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
