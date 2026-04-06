"""X402 Action Provider — credit delegation and x402 proxy actions for AI agents."""

from __future__ import annotations

import json
import re
import time
import secrets
from typing import Any, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator
from coinbase_agentkit import ActionProvider, EvmWalletProvider, Network, create_action
from web3 import Web3

from .constants import (
    BASE_MAINNET_MATCHER,
    ERC20_ABI,
)
from .utils import (
    format_bps,
    format_token_amount,
    format_address,
    format_duration,
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
        import urllib.request
        import urllib.error

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
                return {"status": resp.status, "body": json.loads(resp.read()), "headers": {k.lower(): v for k, v in resp.headers.items()}}
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
        encoded = contract.encode_abi(fn_name="approve", args=[spender_address, required_amount])
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
            usdc_decimals = 6
            borrow_limit_raw = int(float(args["borrow_limit"])) * (10 ** usdc_decimals)
            max_rate_bps = int(args["max_rate_bps"])
            expiry_ts = int(time.time()) + int(args["expiry_days"]) * 86400

            contract = _w3.eth.contract(abi=OPERATOR_ABI)
            encoded = contract.encode_abi(
                fn_name="setOperator",
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
            encoded = contract.encode_abi(fn_name="revokeOperator", args=[args["facilitator_address"]])
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
            limit = int(args.get("limit", "20"))
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


# ── Factory ──────────────────────────────────────────────────────────────────

def x402_action_provider(config: X402Config | None = None) -> X402ActionProvider:
    return X402ActionProvider(config)
