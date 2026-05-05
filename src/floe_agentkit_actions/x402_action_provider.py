"""X402 Action Provider — credit delegation and x402 proxy actions for AI agents."""

from __future__ import annotations

import json
import re
import secrets
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlparse

from coinbase_agentkit import ActionProvider, EvmWalletProvider, create_action
from coinbase_agentkit.network import Network
from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from .constants import (
    BASE_MAINNET_MATCHER,
    ERC20_ABI,
)
from .utils import (
    format_address,
    format_bps,
    format_duration,
    format_token_amount,
)

# ── ABI fragments for operator functions ────────────────────────────────────

OPERATOR_ABI: list[dict[str, Any]] = [
    {
        "name": "setOperator",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "operator", "type": "address"},
            {"name": "borrowLimit", "type": "uint256"},
            {"name": "maxRateBps", "type": "uint256"},
            {"name": "expiry", "type": "uint256"},
            {"name": "onBehalfOfRestriction", "type": "address"},
        ],
        "outputs": [],
    },
    {
        "name": "revokeOperator",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "operator", "type": "address"}],
        "outputs": [],
    },
    {
        "name": "getOperatorPermission",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "agent", "type": "address"},
            {"name": "operator", "type": "address"},
        ],
        "outputs": [
            {
                "type": "tuple",
                "components": [
                    {"name": "approved", "type": "bool"},
                    {"name": "borrowLimit", "type": "uint256"},
                    {"name": "borrowed", "type": "uint256"},
                    {"name": "maxRateBps", "type": "uint256"},
                    {"name": "expiry", "type": "uint256"},
                    {"name": "onBehalfOfRestriction", "type": "address"},
                ],
            }
        ],
    },
]

_w3 = Web3()
_ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
_ADDRESS_PATTERN = r"^0x[a-fA-F0-9]{40}$"


def _validate_address(v: str) -> str:
    if not re.match(_ADDRESS_PATTERN, v):
        raise ValueError("Must be a valid Ethereum address (0x + 40 hex chars)")
    return v


# ── Schemas ──────────────────────────────────────────────────────────────────


class GrantCreditDelegationSchema(BaseModel):
    facilitator_address: str = Field(description="The facilitator's operator address")
    facilitator_url: str = Field(description="The facilitator API base URL")
    borrow_limit: str = Field(description="Maximum borrow limit in USDC (e.g. '10000' for $10K)")
    max_rate_bps: str = Field(default="1500", description="Maximum interest rate in basis points")
    expiry_days: str = Field(default="90", description="Number of days until delegation expires")
    collateral_token: str = Field(description="Collateral token address (WETH or cbBTC)")

    @field_validator("facilitator_address", "collateral_token")
    @classmethod
    def validate_addresses(cls, v: str) -> str:
        return _validate_address(v)


class RevokeCreditDelegationSchema(BaseModel):
    facilitator_address: str = Field(description="The facilitator's operator address to revoke")

    @field_validator("facilitator_address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        return _validate_address(v)


class CheckCreditDelegationSchema(BaseModel):
    facilitator_address: str = Field(description="The facilitator's operator address to check")

    @field_validator("facilitator_address")
    @classmethod
    def validate_address(cls, v: str) -> str:
        return _validate_address(v)


class X402FetchSchema(BaseModel):
    url: str = Field(description="The URL to fetch (may require x402 payment)")
    method: str = Field(default="GET", description="HTTP method")
    headers: Optional[dict[str, str]] = Field(default=None, description="Optional HTTP headers")
    body: Optional[str] = Field(default=None, description="Optional request body")


class X402GetBalanceSchema(BaseModel):
    pass


class X402GetTransactionsSchema(BaseModel):
    limit: str = Field(default="20", description="Number of transactions to return")


# ── Agent Awareness schemas ──────────────────────────────────────────────────
# The 9 actions below let an agent reason about its own credit before
# committing capital. Identity comes from the configured facilitator_api_key
# (Bearer), so none of these accept a wallet address parameter.


class GetCreditRemainingSchema(BaseModel):
    pass


class GetLoanStateSchema(BaseModel):
    pass


class GetSpendLimitSchema(BaseModel):
    pass


class SetSpendLimitSchema(BaseModel):
    limit_raw: str = Field(
        description="Session spend cap in raw USDC units (6 decimals). e.g. '1000000' = $1.",
    )

    @field_validator("limit_raw")
    @classmethod
    def validate_positive_int(cls, v: str) -> str:
        if not re.match(r"^[1-9]\d*$", v):
            raise ValueError("Must be a positive integer (raw USDC, 6 decimals)")
        return v


class ClearSpendLimitSchema(BaseModel):
    pass


class ListCreditThresholdsSchema(BaseModel):
    pass


class RegisterCreditThresholdSchema(BaseModel):
    threshold_bps: int = Field(
        ge=1,
        le=10000,
        description="Utilization threshold in bps (5000 = 50%, 9500 = 95% triggers credit.at_limit).",
    )
    webhook_id: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional webhook id to pin to (must be owned by this developer). Omit for fanout.",
    )


class DeleteCreditThresholdSchema(BaseModel):
    id: int = Field(ge=1, description="Threshold subscription id from list_credit_thresholds.")


class EstimateX402CostSchema(BaseModel):
    url: str = Field(description="Target x402-protected URL to preflight.")
    method: Optional[str] = Field(default=None, description="HTTP method (default GET).")

    @field_validator("method")
    @classmethod
    def validate_method(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and not re.match(r"^[A-Z]{3,7}$", v):
            raise ValueError("Method must be 3-7 uppercase letters")
        return v


# ── Config ───────────────────────────────────────────────────────────────────


class X402Config:
    def __init__(
        self,
        facilitator_url: str = "",
        facilitator_api_key: str = "",
        matcher_address: str = BASE_MAINNET_MATCHER,
    ) -> None:
        self.facilitator_url = facilitator_url
        self.facilitator_api_key = facilitator_api_key
        self.matcher_address = matcher_address


# ── Provider ─────────────────────────────────────────────────────────────────


class X402ActionProvider(ActionProvider[EvmWalletProvider]):
    def __init__(self, config: X402Config | None = None) -> None:
        super().__init__("x402", [])
        cfg = config or X402Config()
        self._matcher_address = cfg.matcher_address
        self._facilitator_url = cfg.facilitator_url
        self._facilitator_api_key = cfg.facilitator_api_key

    def supports_network(self, network: Network) -> bool:
        return network.chain_id in ("8453", "84532")

    def _facilitator_fetch(self, path: str, method: str = "GET", body: Any = None) -> dict[str, Any]:
        import urllib.error
        import urllib.request

        if not self._facilitator_url:
            raise ValueError("facilitator_url not configured")

        url = f"{self._facilitator_url}{path}"
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Invalid URL scheme: {parsed.scheme}")

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._facilitator_api_key}",
        }
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return {
                    "status": resp.status,
                    "body": json.loads(resp.read()),
                    "headers": {k.lower(): v for k, v in resp.headers.items()},
                }
        except urllib.error.HTTPError as e:
            body_text = e.read().decode() if e.fp else ""
            try:
                return {"status": e.code, "body": json.loads(body_text), "headers": {}}
            except json.JSONDecodeError:
                return {"status": e.code, "body": {"error": body_text}, "headers": {}}

    def _ensure_allowance(
        self,
        wallet_provider: EvmWalletProvider,
        token_address: str,
        spender_address: str,
        required_amount: int,
    ) -> str | None:
        owner = wallet_provider.get_address()
        current = wallet_provider.read_contract(
            contract_address=token_address,
            abi=ERC20_ABI,
            function_name="allowance",
            args=[owner, spender_address],
        )
        if int(current) >= required_amount:
            return None
        contract = _w3.eth.contract(abi=ERC20_ABI)
        encoded = contract.encode_abi("approve", args=[spender_address, required_amount])
        return wallet_provider.send_transaction(transaction={"to": token_address, "data": encoded})

    # ── grant_credit_delegation ────────────────────────────────────────────

    @create_action(
        name="grant_credit_delegation",
        description=(
            "Grant credit delegation to an x402 facilitator. This allows the facilitator to borrow "
            "USDC on your behalf using your collateral. You set a maximum borrow limit, interest rate "
            "cap, and expiry. This action handles pre-registration, setOperator, collateral approval, "
            "and registration in one step."
        ),
        schema=GrantCreditDelegationSchema,
    )
    def grant_credit_delegation(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            # Validate local inputs BEFORE hitting the facilitator. A
            # malformed borrow_limit / max_rate_bps / expiry_days previously
            # would still trigger /agents/pre-register (creating Privy
            # state on the facilitator side) before the local validation
            # rejected the call. Fail fast to keep the facilitator clean.
            #
            # Precise decimal→USDC raw conversion. Parsing via float is a
            # money-math bug: `int(float("1.5")) == 1` silently drops $0.50,
            # and larger amounts can lose more to float rounding. Use Decimal
            # end-to-end so fractional inputs like "1500.25" produce
            # 1_500_250_000 raw units exactly.
            usdc_decimals = 6
            try:
                borrow_limit_decimal = Decimal(str(args["borrow_limit"]))
                max_rate_bps = int(args["max_rate_bps"])
                expiry_days = int(args["expiry_days"])
            except KeyError as e:
                return f"Invalid delegation input: missing required field {e.args[0]!r}"
            except (InvalidOperation, TypeError, ValueError) as e:
                return f"Invalid delegation input: {e}"
            if borrow_limit_decimal <= 0:
                return (
                    f"borrow_limit must be positive, got '{args['borrow_limit']}'. "
                    "A zero or negative credit line cannot be delegated."
                )
            if max_rate_bps <= 0:
                return f"max_rate_bps must be positive, got {max_rate_bps}."
            if expiry_days <= 0:
                return f"expiry_days must be positive, got {expiry_days}."
            scaled = borrow_limit_decimal * (Decimal(10) ** usdc_decimals)
            if scaled != scaled.to_integral_value():
                return (
                    f"borrow_limit '{args['borrow_limit']}' has more precision than "
                    f"USDC supports ({usdc_decimals} decimals). Reduce the precision."
                )
            borrow_limit_raw = int(scaled)
            expiry_ts = int(time.time()) + expiry_days * 86400
            # Bound-check against uint256 before encoding calldata. Python's
            # arbitrary-precision int otherwise lets oversized inputs through
            # local validation only to fail later at ABI encode time with a
            # less-actionable error. All three fields are uint256 in OPERATOR_ABI.
            max_uint256 = (1 << 256) - 1
            for field_name, value in (
                ("borrow_limit", borrow_limit_raw),
                ("max_rate_bps", max_rate_bps),
                ("expiry", expiry_ts),
            ):
                if value > max_uint256:
                    return f"{field_name} is too large for uint256 ({value})."

            agent_address = wallet_provider.get_address()
            facilitator_url = args["facilitator_url"]
            # Set facilitator URL before first API call
            self._facilitator_url = facilitator_url

            # Step 1: Pre-register
            nonce = f"{int(time.time())}-{secrets.token_hex(8)}"
            sign_message = f"Register with Floe Facilitator\nNonce: {nonce}"
            signature = wallet_provider.sign_message(sign_message)

            pre_reg = self._facilitator_fetch("/agents/pre-register", "POST", {
                "walletAddress": agent_address, "signature": signature, "nonce": nonce,
            })
            if pre_reg["status"] != 201 and pre_reg["status"] != 200:
                return f"Pre-registration failed: {pre_reg['body'].get('error', 'Unknown error')}"

            privy_wallet = pre_reg["body"]["privyWalletAddress"]

            # Step 2: setOperator

            contract = _w3.eth.contract(abi=OPERATOR_ABI)
            encoded = contract.encode_abi("setOperator",
                args=[args["facilitator_address"], borrow_limit_raw, max_rate_bps, expiry_ts, privy_wallet],
            )
            set_op_tx = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": encoded}
            )

            # Step 3: Approve collateral (max approval — agent controls via delegation limits)
            approval_amount = 2**256 - 1  # type(uint256).max
            approve_tx = self._ensure_allowance(
                wallet_provider, args["collateral_token"], self._matcher_address, approval_amount,
            )

            # Step 4: Register
            reg_nonce = f"{int(time.time())}-{secrets.token_hex(8)}"
            reg_message = f"Register with Floe Facilitator\nNonce: {reg_nonce}"
            reg_signature = wallet_provider.sign_message(reg_message)

            reg = self._facilitator_fetch("/agents/register", "POST", {
                "walletAddress": agent_address, "signature": reg_signature, "nonce": reg_nonce,
            })
            if reg["status"] != 201 and reg["status"] != 200:
                return (
                    f"Registration failed (delegation set on-chain): {reg['body'].get('error', '')}. "
                    "Retry registration later — on-chain delegation is active."
                )

            result = reg["body"]
            self._facilitator_api_key = result.get("apiKey", "")
            self._facilitator_url = facilitator_url

            credit_limit = format_token_amount(int(result.get("creditLimit", "0")), usdc_decimals, "USDC")

            lines = [
                "## Credit Delegation Granted\n",
                f"**Facilitator**: {format_address(args['facilitator_address'])}",
                f"**Privy Wallet**: {format_address(result.get('privyWalletAddress', ''))}",
                f"**Credit Limit**: {credit_limit}",
                f"**Max Rate**: {format_bps(max_rate_bps)} APR",
                f"**Expires**: {format_duration(int(args['expiry_days']) * 86400)}",
                "",
                f"**setOperator tx**: {set_op_tx}",
                f"**Approval tx**: {approve_tx}" if approve_tx else "**Approval**: Already sufficient",
                "",
                f"> **API Key**: `{result.get('apiKey', '')}`",
                "> Save this key — it won't be shown again.",
            ]
            return "\n".join(lines)
        except Exception as e:
            return f"Error granting credit delegation: {e}"

    # ── revoke_credit_delegation ───────────────────────────────────────────

    @create_action(
        name="revoke_credit_delegation",
        description=(
            "Immediately revoke credit delegation from a facilitator. "
            "The facilitator will no longer be able to register new borrow intents on your behalf."
        ),
        schema=RevokeCreditDelegationSchema,
    )
    def revoke_credit_delegation(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            contract = _w3.eth.contract(abi=OPERATOR_ABI)
            encoded = contract.encode_abi("revokeOperator", args=[args["facilitator_address"]])
            tx_hash = wallet_provider.send_transaction(
                transaction={"to": self._matcher_address, "data": encoded}
            )

            agent_address = wallet_provider.get_address()
            perm = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=OPERATOR_ABI,
                function_name="getOperatorPermission",
                args=[agent_address, args["facilitator_address"]],
            )

            if perm[0]:  # approved
                return f"Warning: revokeOperator tx sent ({tx_hash}) but still shows approved. May not have confirmed."

            return "\n".join([
                "## Credit Delegation Revoked\n",
                f"**Facilitator**: {format_address(args['facilitator_address'])}",
                f"**Transaction**: {tx_hash}",
                "",
                "The facilitator can no longer register new borrow intents on your behalf.",
            ])
        except Exception as e:
            return f"Error revoking delegation: {e}"

    # ── check_credit_delegation ────────────────────────────────────────────

    @create_action(
        name="check_credit_delegation",
        description=(
            "Check the status of your credit delegation to a facilitator. "
            "Shows approval status, borrowed vs limit, rate cap, expiry, and fund routing."
        ),
        schema=CheckCreditDelegationSchema,
    )
    def check_credit_delegation(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            agent_address = wallet_provider.get_address()
            perm = wallet_provider.read_contract(
                contract_address=self._matcher_address,
                abi=OPERATOR_ABI,
                function_name="getOperatorPermission",
                args=[agent_address, args["facilitator_address"]],
            )

            approved, borrow_limit, borrowed, max_rate_bps, expiry, on_behalf_of = perm
            now = int(time.time())
            is_expired = now > int(expiry)
            days_left = 0 if is_expired else (int(expiry) - now) // 86400
            available = max(0, int(borrow_limit) - int(borrowed))
            near_expiry = 0 < days_left < 7
            usdc_decimals = 6

            lines = [
                "## Credit Delegation Status\n",
                f"**Facilitator**: {format_address(args['facilitator_address'])}",
                f"**Status**: {'⚠️ Expired' if is_expired else ('✅ Active' if approved else '❌ Not Active')}",
                "",
                f"**Borrow Limit**: {format_token_amount(int(borrow_limit), usdc_decimals, 'USDC')}",
                f"**Borrowed**: {format_token_amount(int(borrowed), usdc_decimals, 'USDC')}",
                f"**Available**: {format_token_amount(available, usdc_decimals, 'USDC')}",
                f"**Max Rate**: {format_bps(int(max_rate_bps))} APR",
                f"**Expiry**: {'EXPIRED' if is_expired else f'{days_left} days remaining'}",
            ]

            if str(on_behalf_of) != _ZERO_ADDRESS:
                lines.append(f"**Funds Route To**: {format_address(str(on_behalf_of))}")
            if near_expiry:
                lines.append("\n⚠️ **Delegation expiring soon!** Renew via `grant_credit_delegation`.")
            if is_expired and approved:
                lines.append("\n⚠️ **Delegation is expired.** No new borrows can be made.")

            return "\n".join(lines)
        except Exception as e:
            return f"Error checking delegation: {e}"

    # ── x402_fetch ─────────────────────────────────────────────────────────

    @create_action(
        name="x402_fetch",
        description=(
            "Fetch a URL through the x402 facilitator proxy. If the URL requires payment (HTTP 402), "
            "the facilitator pays automatically using your credit line."
        ),
        schema=X402FetchSchema,
    )
    def x402_fetch(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            resp = self._facilitator_fetch("/proxy/fetch", "POST", {
                "url": args["url"],
                "method": args.get("method", "GET"),
                "headers": args.get("headers"),
                "body": args.get("body"),
            })

            if resp["status"] >= 400:
                error = resp["body"].get("error", "Unknown error")
                error_map = {
                    "funding_in_progress": "⏳ Funding in progress — retry in 30 seconds.",
                    "credit_frozen": "❄️ Credit frozen — collateral health ratio too low.",
                    "insufficient_balance": "💸 Insufficient credit — credit line fully utilized.",
                }
                return error_map.get(error, f"Facilitator error: {error}")

            body_text = json.dumps(resp["body"], indent=2) if isinstance(resp["body"], dict) else str(resp["body"])
            payment_tx = resp["headers"].get("payment-response") or resp["headers"].get("x-payment-response")

            lines = ["## Response\n"]
            if payment_tx:
                lines.append(f"*Paid via x402 — tx: {payment_tx}*\n")
            lines.extend(["```", body_text[:4000], "```"])
            return "\n".join(lines)
        except Exception as e:
            return f"Error fetching URL: {e}"

    # ── x402_get_balance ───────────────────────────────────────────────────

    @create_action(
        name="x402_get_balance",
        description="Check your x402 credit status — available credit, active loans, and health ratio.",
        schema=X402GetBalanceSchema,
    )
    def x402_get_balance(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            resp = self._facilitator_fetch("/agents/balance")
            if resp["status"] >= 400:
                return f"Error: {resp['body'].get('error', 'Unknown')}"

            data = resp["body"]
            usdc_decimals = 6
            return "\n".join([
                "## x402 Credit Status\n",
                f"**Credit Limit**: {format_token_amount(int(data.get('creditLimit', '0')), usdc_decimals, 'USDC')}",
                f"**Credit Used**: {format_token_amount(int(data.get('creditUsed', '0')), usdc_decimals, 'USDC')}",
                f"**Credit Available**: {format_token_amount(int(data.get('creditAvailable', '0')), usdc_decimals, 'USDC')}",
                f"**Active Loans**: {len(data.get('activeLoans', []))}",
                f"**Delegation Active**: {'✅ Yes' if data.get('delegationActive') else '❌ No'}",
            ])
        except Exception as e:
            return f"Error fetching balance: {e}"

    # ── x402_get_transactions ──────────────────────────────────────────────

    @create_action(
        name="x402_get_transactions",
        description="Get your recent x402 payment history — URLs accessed, amounts paid, and tx hashes.",
        schema=X402GetTransactionsSchema,
    )
    def x402_get_transactions(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            # Clamp limit to <=100 and default to 20 for non-numeric/negative/zero input.
            # Mirrors the TypeScript port's commits 40cc356 / b4bcb3c.
            try:
                parsed = int(args.get("limit", "20"))
                limit = min(parsed, 100) if parsed > 0 else 20
            except (ValueError, TypeError):
                limit = 20
            resp = self._facilitator_fetch(f"/agents/transactions?limit={limit}")
            if resp["status"] >= 400:
                return f"Error: {resp['body'].get('error', 'Unknown')}"

            txns = resp["body"].get("transactions", [])
            if not txns:
                return "No transactions found."

            usdc_decimals = 6
            lines = ["## Recent Transactions\n"]
            for tx in txns:
                amount = format_token_amount(int(tx.get("paymentAmountRaw", "0")), usdc_decimals, "USDC") if tx.get("paymentAmountRaw") else "—"
                status_icon = {"success": "✅", "passthrough": "🔄"}.get(tx.get("status", ""), "❌")
                lines.append(f"{status_icon} **{tx.get('method', '?')}** {tx.get('targetUrl', '?')}")
                lines.append(f"   Amount: {amount} | {tx.get('createdAt', '?')}")
                if tx.get("x402TxHash"):
                    lines.append(f"   Tx: {tx['x402TxHash']}")

            if resp["body"].get("hasMore"):
                lines.append("\n*More transactions available.*")
            return "\n".join(lines)
        except Exception as e:
            return f"Error fetching transactions: {e}"

    # ════════════════════════════════════════════════════════════════════════
    # AGENT AWARENESS (9) — answer "do I have credit?", "is this call worth
    # it?", "where am I in the loan lifecycle?" before committing capital.
    # All require facilitator_api_key to be set on the provider config.
    # ════════════════════════════════════════════════════════════════════════

    # ── get_credit_remaining ───────────────────────────────────────────────

    @create_action(
        name="get_credit_remaining",
        description=(
            "Return the calling agent's current credit headroom: available USDC, "
            "headroomToAutoBorrow, utilizationBps, and any active session spend-limit. "
            "Use BEFORE deciding whether to make a paid call."
        ),
        schema=GetCreditRemainingSchema,
    )
    def get_credit_remaining(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            resp = self._facilitator_fetch("/agents/credit-remaining")
            if resp["status"] >= 400:
                return f"Error: {resp['body'].get('error', resp['body'].get('detail', 'Unknown'))}"
            d = resp["body"]
            usdc = 6
            lines = [
                "## Credit Remaining\n",
                f"**Available**: {format_token_amount(int(d.get('available', '0')), usdc, 'USDC')}",
                f"**Credit Limit**: {format_token_amount(int(d.get('creditLimit', '0')), usdc, 'USDC')}",
                f"**Headroom to Auto-Borrow**: {format_token_amount(int(d.get('headroomToAutoBorrow', '0')), usdc, 'USDC')}",
                f"**Utilization**: {format_bps(int(d.get('utilizationBps', 0)))}",
            ]
            if d.get("sessionSpendLimit"):
                lines.append(
                    f"**Session Cap**: {format_token_amount(int(d['sessionSpendLimit']), usdc, 'USDC')} "
                    f"(remaining {format_token_amount(int(d.get('sessionSpendRemaining') or '0'), usdc, 'USDC')})"
                )
            return "\n".join(lines)
        except Exception as e:
            return f"Error fetching credit-remaining: {e}"

    # ── get_loan_state ─────────────────────────────────────────────────────

    @create_action(
        name="get_loan_state",
        description=(
            "Return the agent's coarse loan state-machine view: idle | borrowing | at_limit | repaying. "
            "Use to gate actions that only make sense in specific states (e.g. don't spend while at_limit)."
        ),
        schema=GetLoanStateSchema,
    )
    def get_loan_state(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            resp = self._facilitator_fetch("/agents/loan-state")
            if resp["status"] >= 400:
                return f"Error: {resp['body'].get('error', resp['body'].get('detail', 'Unknown'))}"
            d = resp["body"]
            lines = [
                "## Loan State\n",
                f"**State**: {d.get('state', 'unknown')}",
                f"**Reason**: {d.get('reason', '—')}",
            ]
            if d.get("details"):
                lines.append(f"**Details**: {json.dumps(d['details'], indent=2)}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error fetching loan-state: {e}"

    # ── get_spend_limit ────────────────────────────────────────────────────

    @create_action(
        name="get_spend_limit",
        description=(
            "Return the agent's currently-active session spend cap, if any. "
            "Returns inactive when no cap is set."
        ),
        schema=GetSpendLimitSchema,
    )
    def get_spend_limit(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            resp = self._facilitator_fetch("/agents/spend-limit")
            if resp["status"] >= 400:
                return f"Error: {resp['body'].get('error', resp['body'].get('detail', 'Unknown'))}"
            d = resp["body"]
            if not d.get("active"):
                return "## Spend Limit\n\nNo session spend cap set."
            usdc = 6
            return "\n".join([
                "## Spend Limit\n",
                f"**Cap**: {format_token_amount(int(d.get('limitRaw') or '0'), usdc, 'USDC')}",
                f"**Spent this session**: {format_token_amount(int(d.get('sessionSpentRaw') or '0'), usdc, 'USDC')}",
                f"**Remaining**: {format_token_amount(int(d.get('sessionRemainingRaw') or '0'), usdc, 'USDC')}",
            ])
        except Exception as e:
            return f"Error fetching spend-limit: {e}"

    # ── set_spend_limit ────────────────────────────────────────────────────

    @create_action(
        name="set_spend_limit",
        description=(
            "Set or update the agent's session spend cap (raw USDC, 6 decimals). Resets the session "
            "window — anything spent before this call no longer counts. Operator-defined; distinct "
            "from the on-chain creditLimit."
        ),
        schema=SetSpendLimitSchema,
    )
    def set_spend_limit(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            resp = self._facilitator_fetch(
                "/agents/spend-limit",
                method="PUT",
                body={"limitRaw": args["limit_raw"]},
            )
            if resp["status"] >= 400:
                return f"Error: {resp['body'].get('error', resp['body'].get('detail', 'Unknown'))}"
            d = resp["body"]
            return "\n".join([
                "## Spend Limit Set\n",
                f"**Cap**: {format_token_amount(int(d.get('limitRaw', '0')), 6, 'USDC')}",
                f"**Session Started**: {d.get('sessionStartedAt', '—')}",
            ])
        except Exception as e:
            return f"Error setting spend-limit: {e}"

    # ── clear_spend_limit ──────────────────────────────────────────────────

    @create_action(
        name="clear_spend_limit",
        description=(
            "Remove the agent's session spend cap. Subsequent paid calls will only be bounded "
            "by the on-chain creditLimit."
        ),
        schema=ClearSpendLimitSchema,
    )
    def clear_spend_limit(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            resp = self._facilitator_fetch("/agents/spend-limit", method="DELETE")
            if resp["status"] >= 400:
                return f"Error: {resp['body'].get('error', resp['body'].get('detail', 'Unknown'))}"
            return "## Spend Limit Cleared\n\nNo cap is now active."
        except Exception as e:
            return f"Error clearing spend-limit: {e}"

    # ── list_credit_thresholds ─────────────────────────────────────────────

    @create_action(
        name="list_credit_thresholds",
        description=(
            "List the agent's registered credit-utilization thresholds. Each fires a "
            "credit.warning / credit.at_limit / credit.recovered webhook when crossed."
        ),
        schema=ListCreditThresholdsSchema,
    )
    def list_credit_thresholds(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            resp = self._facilitator_fetch("/agents/credit-thresholds")
            if resp["status"] >= 400:
                return f"Error: {resp['body'].get('error', resp['body'].get('detail', 'Unknown'))}"
            subs = resp["body"].get("subscriptions", [])
            if not subs:
                return "## Credit Thresholds\n\nNone registered."
            lines = ["## Credit Thresholds\n"]
            for s in subs:
                line = (
                    f"**#{s.get('id')}** {format_bps(int(s.get('thresholdBps', 0)))} — "
                    f"state: {s.get('lastState', 'below')}"
                )
                if s.get("webhookId") is not None:
                    line += f" (pinned webhook {s['webhookId']})"
                if s.get("lastFiredAt"):
                    line += f" (last fired {s['lastFiredAt']})"
                lines.append(line)
            return "\n".join(lines)
        except Exception as e:
            return f"Error listing thresholds: {e}"

    # ── register_credit_threshold ──────────────────────────────────────────

    @create_action(
        name="register_credit_threshold",
        description=(
            "Register a credit-utilization threshold. When utilizationBps crosses thresholdBps "
            "from below, the agent's webhook receives credit.warning (or credit.at_limit if >= 9500). "
            "Drops below → credit.recovered. Cap of 20 thresholds per agent."
        ),
        schema=RegisterCreditThresholdSchema,
    )
    def register_credit_threshold(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            body: dict[str, Any] = {"thresholdBps": args["threshold_bps"]}
            if args.get("webhook_id") is not None:
                body["webhookId"] = args["webhook_id"]
            resp = self._facilitator_fetch("/agents/credit-thresholds", method="POST", body=body)
            if resp["status"] >= 400:
                return f"Error: {resp['body'].get('error', resp['body'].get('detail', 'Unknown'))}"
            d = resp["body"]
            tail = (
                f" (pinned webhook {d['webhookId']})"
                if d.get("webhookId") is not None
                else " (fanout to all credit.* webhooks)"
            )
            return "\n".join([
                "## Credit Threshold Registered\n",
                f"**#{d.get('id')}** at {format_bps(int(d.get('thresholdBps', 0)))} — state: {d.get('lastState', 'below')}{tail}",
            ])
        except Exception as e:
            return f"Error registering threshold: {e}"

    # ── delete_credit_threshold ────────────────────────────────────────────

    @create_action(
        name="delete_credit_threshold",
        description="Delete one of the agent's credit-utilization thresholds by id (from list_credit_thresholds).",
        schema=DeleteCreditThresholdSchema,
    )
    def delete_credit_threshold(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            resp = self._facilitator_fetch(
                f"/agents/credit-thresholds/{args['id']}",
                method="DELETE",
            )
            if resp["status"] >= 400:
                return f"Error: {resp['body'].get('error', resp['body'].get('detail', 'Unknown'))}"
            return f"## Credit Threshold Deleted\n\nThreshold #{args['id']} removed."
        except Exception as e:
            return f"Error deleting threshold: {e}"

    # ── estimate_x402_cost ─────────────────────────────────────────────────

    @create_action(
        name="estimate_x402_cost",
        description=(
            "Preflight an x402-protected URL and return its USDC cost without paying. Reflects "
            "against the calling agent's available credit and session spend-limit so you can decide "
            "gating in one round-trip. Use BEFORE x402_fetch."
        ),
        schema=EstimateX402CostSchema,
    )
    def estimate_x402_cost(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            body: dict[str, Any] = {"url": args["url"]}
            if args.get("method"):
                body["method"] = args["method"]
            resp = self._facilitator_fetch("/x402/estimate", method="POST", body=body)
            if resp["status"] >= 400:
                return f"Error: {resp['body'].get('error', resp['body'].get('detail', 'Unknown'))}"
            d = resp["body"]
            method = d.get("method", "GET")
            url = d.get("url", "")
            if not d.get("x402"):
                return f"## x402 Estimate\n\n**{method} {url}** is not x402-protected — no payment required."
            usdc = 6
            lines = [
                "## x402 Estimate\n",
                f"**{method} {url}**",
                f"**Price**: {format_token_amount(int(d.get('priceRaw', '0')), usdc, 'USDC')}",
                f"**Network**: {d.get('network', '—')}",
                f"**Pay To**: {format_address(d['payTo']) if d.get('payTo') else '—'}",
                f"**Cached**: {'yes' if d.get('cached') else 'no'}",
            ]
            r = d.get("reflection")
            if r:
                lines.extend([
                    "",
                    "### Decision",
                    f"**Available**: {format_token_amount(int(r.get('available', '0')), usdc, 'USDC')}",
                    f"**Would exceed available?**: {'YES — DO NOT CALL' if r.get('willExceedAvailable') else 'no'}",
                    f"**Would exceed auto-borrow headroom?**: {'YES' if r.get('willExceedHeadroom') else 'no'}",
                    f"**Would exceed session spend-limit?**: {'YES — DO NOT CALL' if r.get('willExceedSpendLimit') else 'no'}",
                ])
            return "\n".join(lines)
        except Exception as e:
            return f"Error estimating cost: {e}"


# ── Factory ──────────────────────────────────────────────────────────────────

def x402_action_provider(config: X402Config | None = None) -> X402ActionProvider:
    return X402ActionProvider(config)
