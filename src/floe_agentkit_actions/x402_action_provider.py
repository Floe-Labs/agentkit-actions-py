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

# Whitelisted collateral tokens. Mirrors agentkit-actions (TypeScript)'s
# KNOWN_COLLATERAL set. Restricting to these two specifically lets the
# bounded-approval handler skip the USDT-style approve(0)-first dance —
# both WETH and cbBTC are standard ERC-20s that accept a direct
# approve(N) regardless of the prior allowance.
_KNOWN_COLLATERAL: frozenset[str] = frozenset(
    {
        "0x4200000000000000000000000000000000000006",  # WETH on Base
        "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",  # cbBTC on Base
    }
)


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
    collateral_approval: Optional[str] = Field(
        default=None,
        description=(
            "Bounded collateral allowance to grant the matcher (raw token units). "
            "Mutually exclusive with `unsafe_infinite_approval`. "
            "If neither field is set, no approve tx is sent — the action returns "
            "the current matcher allowance in its response so the caller can decide "
            "whether to grant one externally via their wallet provider."
        ),
    )
    unsafe_infinite_approval: Optional[bool] = Field(
        default=None,
        description=(
            "Opt in to unlimited (type(uint256).max) collateral approval to the matcher. "
            "Saves one approve() per top-up but means a matcher compromise can drain "
            "your full collateral token balance. Mutually exclusive with `collateral_approval`."
        ),
    )

    @field_validator("facilitator_address")
    @classmethod
    def validate_facilitator_address(cls, v: str) -> str:
        return _validate_address(v)

    @field_validator("collateral_token")
    @classmethod
    def validate_collateral_token(cls, v: str) -> str:
        addr = _validate_address(v)
        if addr.lower() not in _KNOWN_COLLATERAL:
            raise ValueError("Must be WETH or cbBTC")
        return addr


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
            "cap, and expiry. This action: (1) pre-registers with the facilitator, (2) calls "
            "setOperator on the lending contract, (3) optionally approves the collateral token to "
            "the matcher (controlled by `collateral_approval` or `unsafe_infinite_approval` — "
            "neither is set by default; the response reports whether an approve tx was sent and the "
            "current matcher allowance), (4) completes registration with the facilitator."
        ),
        schema=GrantCreditDelegationSchema,
    )
    def grant_credit_delegation(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            # Reject incoherent approval combinations before any side effects
            # (no facilitator pre-register, no on-chain setOperator). The
            # schema accepts both fields so existing callers don't get a
            # validation error from the framework; the choice is enforced here.
            if args.get("collateral_approval") is not None and args.get("unsafe_infinite_approval"):
                return "Cannot set both `collateral_approval` and `unsafe_infinite_approval` — pick one."

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
                # Parse collateral_approval upfront too. Previously this was
                # parsed in Step 3 — after pre-register and setOperator had
                # already happened — so a non-numeric, negative, or oversized
                # value would only surface as a generic catch-all error after
                # irreversible facilitator/on-chain side effects had landed.
                requested_collateral_approval: Optional[int] = (
                    int(args["collateral_approval"]) if args.get("collateral_approval") is not None else None
                )
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
            if requested_collateral_approval is not None and requested_collateral_approval < 0:
                return f"collateral_approval must be non-negative, got '{args['collateral_approval']}'."
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
            # less-actionable error. All four fields are uint256 in
            # OPERATOR_ABI / the ERC-20 approve interface.
            max_uint256 = (1 << 256) - 1
            uint256_checks: list[tuple[str, int]] = [
                ("borrow_limit", borrow_limit_raw),
                ("max_rate_bps", max_rate_bps),
                ("expiry", expiry_ts),
            ]
            if requested_collateral_approval is not None:
                uint256_checks.append(("collateral_approval", requested_collateral_approval))
            for field_name, value in uint256_checks:
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

            # Step 3: Approve collateral. The approve step is skipped unless
            # the caller opts in explicitly. Previously this defaulted to
            # type(uint256).max, which silently granted the matcher unlimited
            # spend power on every delegation grant. Now the caller picks one of:
            #   unsafe_infinite_approval=True → MAX_UINT256 (at-least semantics — no tx if already MAX)
            #   collateral_approval=<raw>     → set to exactly this amount (force-set, including DOWN)
            #   neither set                   → no approve tx; the response reports the current
            #                                   matcher allowance so the caller knows whether their
            #                                   wallet is already bounded or still has a stale
            #                                   infinite grant from the old default
            #
            # The bounded path force-sets rather than using `_ensure_allowance`'s
            # at-least semantics: a caller migrating from the old MAX_UINT256
            # default to a bounded value otherwise no-ops here and walks away
            # with the old infinite allowance still active — exactly the false
            # sense of bounded exposure this PR is meant to remove. WETH and
            # cbBTC (the only collaterals the schema accepts) don't need the
            # USDT-style approve(0)-first dance, so a single direct approve is
            # safe.
            approve_tx: Optional[str] = None
            current_allowance: Optional[int] = None
            if args.get("unsafe_infinite_approval"):
                approval_amount = 2**256 - 1  # type(uint256).max
                approve_tx = self._ensure_allowance(
                    wallet_provider,
                    args["collateral_token"],
                    self._matcher_address,
                    approval_amount,
                )
            else:
                # Read once: the bounded path uses this for the force-set
                # decision, and the neither-set path renders it back to the
                # caller. Reuses `agent_address` from earlier in the handler
                # to avoid a redundant get_address() call.
                current_allowance = int(
                    wallet_provider.read_contract(
                        contract_address=args["collateral_token"],
                        abi=ERC20_ABI,
                        function_name="allowance",
                        args=[agent_address, self._matcher_address],
                    )
                )
                if requested_collateral_approval is not None:
                    if current_allowance != requested_collateral_approval:
                        erc20 = _w3.eth.contract(abi=ERC20_ABI)
                        encoded_approve = erc20.encode_abi(
                            "approve", args=[self._matcher_address, requested_collateral_approval]
                        )
                        approve_tx = wallet_provider.send_transaction(
                            transaction={"to": args["collateral_token"], "data": encoded_approve}
                        )
            approval_requested = (
                args.get("unsafe_infinite_approval") is True or args.get("collateral_approval") is not None
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

            if approval_requested:
                approval_line = (
                    f"**Approval tx**: {approve_tx}" if approve_tx else "**Approval**: Already at requested amount"
                )
            else:
                allowance_str = str(current_allowance) if current_allowance is not None else "unknown"
                approval_line = (
                    f"**Approval**: No approval tx was sent. Current matcher allowance "
                    f"on this token (raw): {allowance_str}. Re-run with "
                    "`collateral_approval=<raw>` or `unsafe_infinite_approval=True` "
                    "to set a new bound, or grant an allowance through your wallet "
                    "provider directly."
                )

            lines = [
                "## Credit Delegation Granted\n",
                f"**Facilitator**: {format_address(args['facilitator_address'])}",
                f"**Privy Wallet**: {format_address(result.get('privyWalletAddress', ''))}",
                f"**Credit Limit**: {credit_limit}",
                f"**Max Rate**: {format_bps(max_rate_bps)} APR",
                f"**Expires**: {format_duration(int(args['expiry_days']) * 86400)}",
                "",
                f"**setOperator tx**: {set_op_tx}",
                approval_line,
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


# ── Factory ──────────────────────────────────────────────────────────────────

def x402_action_provider(config: X402Config | None = None) -> X402ActionProvider:
    return X402ActionProvider(config)
