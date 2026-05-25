"""X402 Action Provider — credit delegation and x402 proxy actions for AI agents."""

from __future__ import annotations

import json
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional
from urllib.parse import urlparse

from coinbase_agentkit import ActionProvider, EvmWalletProvider, create_action
from coinbase_agentkit.network import Network
from pydantic import BaseModel, Field, field_validator
from web3 import Web3

from .constants import BASE_MAINNET_MATCHER, USDC_USDC_MAX_ORIGINATION_LTV_BPS
from .utils import (
    format_address,
    format_bps,
    format_duration,
    format_token_amount,
)

# ── ABI fragments for operator functions ────────────────────────────────────

OPERATOR_ABI: list[dict[str, Any]] = [
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
    name: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "Human-friendly label for this agent (e.g. 'alpha', 'paid-search-bot'). "
            "Unique per developer. Used by the CLI/dashboard to identify the agent later."
        ),
    )
    facilitator_url: str = Field(description="The facilitator API base URL")
    borrow_limit: str = Field(description="Maximum borrow limit in USDC (e.g. '10000' for $10K)")
    max_rate_bps: str = Field(default="1500", description="Maximum interest rate in basis points")
    expiry_days: str = Field(default="90", description="Number of days until delegation expires")

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not re.match(r"^[A-Za-z0-9 _-]+$", v):
            raise ValueError("name must be alphanumeric / space / underscore / hyphen")
        return v

    @field_validator("facilitator_url")
    @classmethod
    def validate_facilitator_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("facilitator_url must use HTTPS")
        return v


class OpenCreditLineSchema(BaseModel):
    name: str = Field(
        min_length=1,
        max_length=64,
        description=(
            "The agent name from `grant_credit_delegation` / `floe-agent register`. "
            "Must already exist server-side."
        ),
    )
    facilitator_url: str = Field(description="The facilitator API base URL (e.g. https://x402.floe.xyz)")
    deposit_usdc: str = Field(description="USDC deposit amount (e.g. '10000' for $10K)")
    max_ltv_bps: int = Field(
        default=9500,
        ge=1,
        le=USDC_USDC_MAX_ORIGINATION_LTV_BPS,
        description=(
            f"Optional LTV cap (1..{USDC_USDC_MAX_ORIGINATION_LTV_BPS}) for the "
            "USDC/USDC credit line. Default 9500 (95%) — the conservative "
            "origination ceiling with ~5% headroom for interest accrual before "
            f"liquidation. Values 9501..{USDC_USDC_MAX_ORIGINATION_LTV_BPS} enable "
            "the aggressive mode, only safe for short-duration loans that you "
            f"repay on a tight cadence: at {USDC_USDC_MAX_ORIGINATION_LTV_BPS} "
            "with a 12% APR loan there is only 50bps of headroom to the 9950bps "
            "liquidation threshold, which interest closes in roughly 15 days."
        ),
    )
    agent_id: int | None = Field(
        default=None,
        ge=1,
        description="Server-issued numeric agent id. Pass when not already known.",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not re.match(r"^[A-Za-z0-9 _-]+$", v):
            raise ValueError("name must be alphanumeric / space / underscore / hyphen")
        return v

    @field_validator("facilitator_url")
    @classmethod
    def validate_facilitator_url(cls, v: str) -> str:
        if not v.startswith("https://"):
            raise ValueError("facilitator_url must use HTTPS")
        return v

    @field_validator("deposit_usdc")
    @classmethod
    def validate_deposit(cls, v: str) -> str:
        # Reject zero up front. The previous pattern `^(0|[1-9]\d*)…`
        # accepted "0" and "0.0" — they would round-trip through
        # _usdc_to_raw_units and fail at the runtime "must be positive"
        # check, which the LLM can't react to as cleanly as a schema-level
        # rejection. Require at least one non-zero digit somewhere.
        if not re.match(r"^(?:[1-9]\d*(?:\.\d+)?|0?\.0*[1-9]\d*)$", v):
            raise ValueError("deposit_usdc must be a positive decimal string")
        return v


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


class X402AwaitSettlementSchema(BaseModel):
    nonce: str = Field(
        ...,
        description=(
            "Reservation nonce returned in the 502 body when x402_fetch failed with "
            "`upstream_paid_request_failed_ambiguous`. The settlement helper polls "
            "until the reservation reaches a terminal state (settled | "
            "payment_rejected | expired_unsettled)."
        ),
    )
    interval_seconds: float = Field(default=2.0, gt=0, le=60, description="Polling interval in seconds.")
    timeout_seconds: float = Field(
        default=900.0, gt=0, le=3600,
        description="Maximum time to wait in seconds before giving up (default 900 = 15 min).",
    )


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
    method: str = Field(default="GET", description="HTTP method (default GET).")

    @field_validator("method")
    @classmethod
    def validate_method(cls, v: str) -> str:
        if not re.match(r"^[A-Z]{3,7}$", v):
            raise ValueError("Method must be 3-7 uppercase letters")
        return v


# ── Config ───────────────────────────────────────────────────────────────────


class X402Config:
    def __init__(
        self,
        facilitator_url: str = "",
        facilitator_api_key: str = "",
        matcher_address: str = BASE_MAINNET_MATCHER,
        agent_name: str = "",
    ) -> None:
        self.facilitator_url = facilitator_url
        self.facilitator_api_key = facilitator_api_key
        self.matcher_address = matcher_address
        self.agent_name = agent_name


# ── Provider ─────────────────────────────────────────────────────────────────


class X402ActionProvider(ActionProvider[EvmWalletProvider]):
    def __init__(self, config: X402Config | None = None) -> None:
        super().__init__("x402", [])
        cfg = config or X402Config()
        self._matcher_address = cfg.matcher_address
        self._facilitator_url = cfg.facilitator_url.rstrip("/")
        self._facilitator_api_key = cfg.facilitator_api_key
        self._agent_name = cfg.agent_name

    def supports_network(self, network: Network) -> bool:
        return network.chain_id in ("8453", "84532")

    def _facilitator_fetch(
        self,
        path: str,
        method: str = "GET",
        body: Any = None,
        timeout_seconds: float = 30,
    ) -> dict[str, Any]:
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
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
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

    # ── grant_credit_delegation ────────────────────────────────────────────

    @create_action(
        name="grant_credit_delegation",
        description=(
            "Register a new Floe credit agent. Floe creates a managed Privy wallet for the agent, "
            "delegates the facilitator on-chain server-side, and returns a scoped API key. "
            "You set a name, maximum borrow limit, interest rate cap, and expiry. "
            "The developer wallet is only used to sign the auth headers — no on-chain transactions are sent. "
            "The returned API key is stored for the rest of this session and used by every other x402 action. "
            "For multi-agent setups, prefer the CLI: `floe-agent register --name <name>` "
            "(stores the key in the OS keychain)."
        ),
        schema=GrantCreditDelegationSchema,
    )
    def grant_credit_delegation(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            usdc_decimals = 6
            try:
                borrow_limit_decimal = Decimal(str(args["borrow_limit"]))
                max_rate_bps = int(args["max_rate_bps"])
                expiry_days = int(args["expiry_days"])
                name = str(args["name"])
                facilitator_url = str(args["facilitator_url"]).rstrip("/")
            except KeyError as e:
                return f"Invalid delegation input: missing required field {e.args[0]!r}"
            except (InvalidOperation, TypeError, ValueError) as e:
                return f"Invalid delegation input: {e}"
            if borrow_limit_decimal <= 0:
                return (
                    f"borrow_limit must be positive, got '{args['borrow_limit']}'. "
                    "A zero or negative credit line cannot be delegated."
                )
            if not (1 <= max_rate_bps <= 10000):
                return f"max_rate_bps must be between 1 and 10000, got {max_rate_bps}."
            if not (1 <= expiry_days <= 3650):
                return f"expiry_days must be between 1 and 3650, got {expiry_days}."
            scaled = borrow_limit_decimal * (Decimal(10) ** usdc_decimals)
            if scaled != scaled.to_integral_value():
                return (
                    f"borrow_limit '{args['borrow_limit']}' has more precision than "
                    f"USDC supports ({usdc_decimals} decimals). Reduce the precision."
                )
            borrow_limit_raw = str(int(scaled))
            expiry_seconds = expiry_days * 86400

            # Set the facilitator URL for downstream actions in this session.
            self._facilitator_url = facilitator_url

            # Step 1: create the managed agent. Server provisions Privy
            # wallet + setOperator() delegation in-flight; we just wait
            # for the result.
            create_resp = self._signed_developer_post(
                wallet_provider,
                "/v1/developer/agents",
                {
                    "name": name,
                    "borrowLimitRaw": borrow_limit_raw,
                    "maxRateBps": max_rate_bps,
                    "expirySeconds": expiry_seconds,
                },
            )
            if create_resp["status"] not in (200, 201):
                err = create_resp["body"]
                detail = (err or {}).get("detail") or (err or {}).get("error") or "unknown error"
                return f"Agent creation failed: {detail}"
            created = create_resp["body"]
            agent_id = int(created["agentId"])

            # Step 2: mint an API key for the freshly-created agent. Auth
            # headers expire after 5 minutes; we re-sign rather than reuse.
            key_resp = self._signed_developer_post(
                wallet_provider,
                f"/v1/developer/agents/{agent_id}/keys",
                {"label": name},
            )
            if key_resp["status"] not in (200, 201):
                err = key_resp["body"]
                detail = (err or {}).get("detail") or (err or {}).get("error") or "unknown error"
                return (
                    f"Agent created (id={agent_id}) but key minting failed: {detail}. "
                    f"Mint a key via the dashboard or `floe-agent rotate {name}` to recover."
                )
            key_body = key_resp["body"]

            # Store for subsequent calls in this session.
            self._facilitator_api_key = key_body["key"]
            self._agent_name = name

            credit_limit = format_token_amount(int(borrow_limit_raw), usdc_decimals, "USDC")
            api_key = key_body["key"]
            key_preview = api_key[-4:]

            lines = [
                "## Floe Agent Registered\n",
                f"**Name**: {name}",
                f"**Agent ID**: {agent_id}",
                f"**Status**: {created.get('status', 'active')}",
                f"**Privy Wallet**: {format_address(created.get('privyWalletAddress', ''))}",
                f"**Credit Limit**: {credit_limit}",
                f"**Max Rate**: {format_bps(max_rate_bps)} APR",
                f"**Expires**: {format_duration(expiry_seconds)}",
                f"**Delegation tx**: {created.get('delegationTxHash', '')}",
                "",
                f"> API key stored for this session (ending ...{key_preview}).",
                f"> For persistent storage across sessions, prefer `floe-agent register --name {name}`"
                " from the CLI — it saves the key to your OS keychain.",
                "",
                "> **Next step:** the agent's Privy wallet has no USDC yet, so its credit line is not yet open.",
                f"> Fund the Privy wallet (`{created.get('privyWalletAddress', '')}`) with USDC, then call `open_credit_line`",
                f"> (or `floe-agent open-credit-line --name {name} --deposit <usdc>` from the CLI).",
            ]
            return "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            return f"Error registering Floe agent: {e}"

    # ── open_credit_line ───────────────────────────────────────────────────

    @create_action(
        name="open_credit_line",
        description=(
            "Open the USDC/USDC credit line for a previously-registered Floe agent. "
            "The agent's Privy wallet must already hold at least `deposit_usdc` USDC "
            "(fund it via the dashboard's Coinbase on-ramp or a direct on-chain transfer first). "
            "Floe server-signs the borrow intent FROM the agent's Privy wallet; the solver matches "
            "it asynchronously and the agent's spendable credit becomes non-zero a few seconds later. "
            "For multi-agent setups, prefer the CLI: "
            "`floe-agent open-credit-line --name <name> --deposit <usdc>`."
        ),
        schema=OpenCreditLineSchema,
    )
    def open_credit_line(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            usdc_decimals = 6
            try:
                deposit_decimal = Decimal(str(args["deposit_usdc"]))
                name = str(args["name"])
                facilitator_url = str(args["facilitator_url"]).rstrip("/")
                max_ltv_bps = int(args.get("max_ltv_bps", 9500))
                explicit_agent_id = args.get("agent_id")
            except KeyError as e:
                return f"Invalid open_credit_line input: missing required field {e.args[0]!r}"
            except (InvalidOperation, TypeError, ValueError) as e:
                return f"Invalid open_credit_line input: {e}"

            if deposit_decimal <= 0:
                return f"deposit_usdc must be positive, got {args['deposit_usdc']!r}."
            scaled = deposit_decimal * (Decimal(10) ** usdc_decimals)
            if scaled != scaled.to_integral_value():
                return (
                    f"deposit_usdc '{args['deposit_usdc']}' has more precision than "
                    f"USDC supports ({usdc_decimals} decimals)."
                )
            deposit_raw = str(int(scaled))

            self._facilitator_url = facilitator_url

            # Resolve agent id by listing developer agents when not supplied.
            agent_id: int
            if explicit_agent_id is not None:
                agent_id = int(explicit_agent_id)
            else:
                list_resp = self._signed_developer_get(wallet_provider, "/v1/developer/agents")
                if list_resp["status"] not in (200, 201):
                    err = list_resp["body"]
                    detail = (err or {}).get("detail") or (err or {}).get("error") or "unknown error"
                    return f"Failed to list agents: {detail}"
                agents = (list_resp["body"] or {}).get("agents", [])
                match = next((a for a in agents if a.get("name") == name), None)
                if not match:
                    return (
                        f"No agent named \"{name}\" found for this developer. "
                        f"Register one with `grant_credit_delegation` first."
                    )
                # API serialises camelCase (`agentId`); `id` is kept as a
                # defensive fallback in case the response shape changes
                # back. Mirrors floe_api_client.create_agent at L103.
                agent_id = int(match.get("agentId") or match["id"])

            open_resp = self._signed_developer_post(
                wallet_provider,
                f"/v1/developer/agents/{agent_id}/open-credit-line",
                {"depositRaw": deposit_raw, "maxLtvBps": max_ltv_bps},
            )
            if open_resp["status"] not in (200, 201):
                err = open_resp["body"]
                detail = (err or {}).get("detail") or (err or {}).get("error") or "unknown error"
                return f"Open credit line failed: {detail}"
            result = open_resp["body"]

            lines = [
                "## Credit Line Submitted\n",
                f"**Agent**: {name} (id={agent_id})",
                f"**Deposit**: {format_token_amount(int(result.get('collateralAmountRaw', '0')), usdc_decimals, 'USDC')}",
                f"**Borrow**: {format_token_amount(int(result.get('principalRaw', '0')), usdc_decimals, 'USDC')}",
                f"**Register tx**: {result.get('registerTxHash', '')}",
            ]
            if result.get("approveTxHash"):
                lines.append(f"**Approve tx**: {result['approveTxHash']}")
            lines.append("")
            lines.append(
                f"> Status: `{result.get('status', 'pending_on_chain')}`. "
                "The solver matches asynchronously; spendable credit becomes non-zero "
                "once status flips to `active` (usually a few seconds)."
            )
            return "\n".join(lines)
        except Exception as e:  # noqa: BLE001
            return f"Error opening credit line: {e}"

    def _signed_developer_get(
        self,
        wallet_provider: EvmWalletProvider,
        path: str,
    ) -> dict[str, Any]:
        """GET a Floe developer route with wallet-signed auth headers."""
        import urllib.error
        import urllib.request

        if not self._facilitator_url:
            raise ValueError("facilitator_url not configured")
        timestamp = str(int(time.time()))
        message = f"Floe Credit API\nTimestamp: {timestamp}"
        signature = wallet_provider.sign_message(message)
        address = wallet_provider.get_address()
        headers = {
            "Content-Type": "application/json",
            "X-Wallet-Address": address,
            "X-Signature": signature,
            "X-Timestamp": timestamp,
        }
        url = f"{self._facilitator_url}{path}"
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Invalid URL scheme: {parsed.scheme}")
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return {"status": resp.status, "body": json.loads(resp.read() or b"null")}
        except urllib.error.HTTPError as err:
            raw = err.read().decode() if err.fp else ""
            try:
                return {"status": err.code, "body": json.loads(raw)}
            except json.JSONDecodeError:
                return {"status": err.code, "body": {"error": raw or err.reason}}

    def _signed_developer_post(
        self,
        wallet_provider: EvmWalletProvider,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        """POST to a Floe developer route with wallet-signed auth headers."""
        import urllib.error
        import urllib.request

        if not self._facilitator_url:
            raise ValueError("facilitator_url not configured")

        timestamp = str(int(time.time()))
        message = f"Floe Credit API\nTimestamp: {timestamp}"
        signature = wallet_provider.sign_message(message)
        address = wallet_provider.get_address()
        headers = {
            "Content-Type": "application/json",
            "X-Wallet-Address": address,
            "X-Signature": signature,
            "X-Timestamp": timestamp,
        }

        url = f"{self._facilitator_url}{path}"
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"Invalid URL scheme: {parsed.scheme}")
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return {"status": resp.status, "body": json.loads(resp.read() or b"null")}
        except urllib.error.HTTPError as err:
            raw = err.read().decode() if err.fp else ""
            try:
                return {"status": err.code, "body": json.loads(raw)}
            except json.JSONDecodeError:
                return {"status": err.code, "body": {"error": raw or err.reason}}

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
                f"**Status**: {'Expired' if is_expired else ('Active' if approved else 'Not Active')}",
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
                lines.append("\n**Delegation expiring soon!** Renew via `grant_credit_delegation`.")
            if is_expired and approved:
                lines.append("\n**Delegation is expired.** No new borrows can be made.")

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
                # FLO-567: surface the reservation nonce on ambiguous 502s
                # so the LLM can call x402_await_settlement instead of
                # retrying (which may double-charge).
                if error == "upstream_paid_request_failed_ambiguous":
                    reservation = resp["body"].get("reservation") or {}
                    nonce = reservation.get("nonce")
                    if nonce:
                        detail = resp["body"].get("detail")
                        msg = (
                            "Payment is in-flight but the upstream response is ambiguous (HTTP 502). "
                            "DO NOT retry — that may double-charge. Use `x402_await_settlement` with "
                            f"this nonce to resolve:\n**nonce**: `{nonce}`"
                        )
                        if detail:
                            msg += f"\n\n_Detail: {detail}_"
                        return msg
                error_map = {
                    "funding_in_progress": "⏳ Funding in progress — retry in 30 seconds.",
                    "credit_frozen": "❄️ Credit frozen — collateral health ratio too low.",
                    "insufficient_balance": "💸 Insufficient credit — credit line fully utilized.",
                }
                return error_map.get(error, f"Facilitator error: {error}")

            body_text = json.dumps(resp["body"], indent=2) if isinstance(resp["body"], dict) else str(resp["body"])
            payment_tx = resp["headers"].get("payment-response") or resp["headers"].get("x-payment-response")
            # FLO-552: surface the dollar amount paid. Prefer the decimal alias
            # X-Floe-Payment-Amount; fall back to formatting the raw-units
            # X-Floe-Cost-USDC so older facilitators still show an amount.
            paid_amount = resp["headers"].get("x-floe-payment-amount")
            if paid_amount is None:
                cost_raw = resp["headers"].get("x-floe-cost-usdc")
                if cost_raw is not None:
                    try:
                        paid_amount = f"{int(cost_raw) / 1_000_000:.6f}"
                    except ValueError:
                        paid_amount = None

            lines = ["## Response\n"]
            if payment_tx or paid_amount:
                parts = []
                if paid_amount:
                    parts.append(f"${paid_amount} USDC")
                if payment_tx:
                    parts.append(f"tx: {payment_tx}")
                lines.append(f"*Paid via x402 — {' — '.join(parts)}*\n")
            lines.extend(["```", body_text[:4000], "```"])
            return "\n".join(lines)
        except Exception as e:
            return f"Error fetching URL: {e}"

    # ── x402_get_balance ───────────────────────────────────────────────────

    @create_action(
        name="x402_get_balance",
        description=(
            "Check your x402 credit status: spendable USDC (what you can pay with right now), "
            "borrowing headroom (how much more you could draw from your credit line), "
            "on-chain wallet USDC, active loans, and delegation state."
        ),
        schema=X402GetBalanceSchema,
    )
    def x402_get_balance(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        try:
            resp = self._facilitator_fetch("/agents/balance")
            if resp["status"] >= 400:
                return f"Error: {resp['body'].get('error', 'Unknown')}"

            data = resp["body"]
            usdc_decimals = 6
            # FLO-567: prefer explicit *Raw fields; fall back to legacy.
            # Mirror the fallback chain in FloeAgent.balance_details() — an
            # older facilitator returns `pendingSettlements` (no -Raw suffix),
            # and without the fallback the action silently hides the
            # in-flight warning even when funds are reserved.
            spendable_raw = data.get("spendableRaw") or data.get("balance") or "0"
            credit_available_raw = data.get("creditAvailableRaw") or data.get("creditAvailable", "0")
            pending_raw = data.get("pendingSettlementsRaw") or data.get("pendingSettlements") or "0"
            wallet_usdc_raw = data.get("walletUsdcRaw")

            def fmt(raw):
                return format_token_amount(int(raw or "0"), usdc_decimals, "USDC")

            lines = [
                "## x402 Credit Status\n",
                f"**Spendable now**: {fmt(spendable_raw)} — what you can pay with right now.",
                f"**Borrowing headroom**: {fmt(credit_available_raw)} — how much more you could draw from your credit line.",
            ]
            if wallet_usdc_raw is not None:
                lines.append(f"**Wallet USDC (on-chain)**: {fmt(wallet_usdc_raw)}")
            lines.extend([
                f"**Credit Limit**: {fmt(data.get('creditLimit', '0'))}",
                f"**Credit Used**: {fmt(data.get('creditUsed', '0'))}",
            ])
            if pending_raw and pending_raw != "0":
                lines.append(
                    f"**Pending settlement**: {fmt(pending_raw)} — use `x402_await_settlement` to resolve."
                )
            lines.extend([
                f"**Active Loans**: {len(data.get('activeLoans', []))}",
                f"**Delegation Active**: {'Yes' if data.get('delegationActive') else 'No'}",
            ])
            return "\n".join(lines)
        except Exception as e:
            return f"Error fetching balance: {e}"

    # ── x402_await_settlement ──────────────────────────────────────────────
    #
    # FLO-567: companion to x402_fetch's 502 ambiguous error. Polls until
    # the reservation reaches a terminal state.

    @create_action(
        name="x402_await_settlement",
        description=(
            "Poll the facilitator until a pending x402 reservation reaches a terminal "
            "state. Use this AFTER an x402_fetch call returned a 502 ambiguous error "
            "with a nonce — do NOT retry the original call (that may double-charge). "
            "Resolves with the final state: settled (paid on-chain), payment_rejected "
            "(credit released), or expired_unsettled (authorization expired)."
        ),
        schema=X402AwaitSettlementSchema,
    )
    def x402_await_settlement(self, wallet_provider: EvmWalletProvider, args: dict) -> str:
        import time as _time
        from urllib.parse import quote as _quote

        nonce = args["nonce"]
        # Honor the caller's requested timeout even when smaller than the
        # polling interval — the per-iteration min(interval, remaining)
        # already caps each sleep so we never overshoot the deadline.
        interval_seconds = max(0.1, float(args.get("interval_seconds", 2.0)))
        timeout_seconds = max(0.1, float(args.get("timeout_seconds", 900.0)))

        deadline = _time.monotonic() + timeout_seconds
        path = f"/agents/reservations/{_quote(nonce, safe='')}"
        last_state: Optional[str] = None
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return (
                    f"Timed out after {timeout_seconds}s waiting for reservation `{nonce}` to settle "
                    f"(last state: `{last_state}`). Call this action again to resume waiting."
                )
            per_call_timeout = max(0.1, min(remaining, 30))
            try:
                resp = self._facilitator_fetch(path, timeout_seconds=per_call_timeout)
            except Exception as e:
                return f"Error polling reservation `{nonce}`: {e}. Call this action again to resume waiting."
            if resp["status"] == 404:
                return (
                    f"Reservation `{nonce}` not found. Verify the nonce belongs to this "
                    "agent and was issued recently."
                )
            if resp["status"] >= 400:
                return f"Error polling reservation: {resp['body'].get('error', 'Unknown')}"

            data = resp["body"]
            last_state = data.get("state", last_state)
            if data.get("terminal"):
                usdc_decimals = 6
                # State-aware heading so payment_rejected / expired_unsettled
                # don't read as successful settlements to the LLM.
                state = data.get("state")
                if state == "settled":
                    heading = "## Reservation settled"
                elif state == "payment_rejected":
                    heading = "## Reservation rejected (credit released)"
                elif state == "expired_unsettled":
                    heading = "## Reservation expired (may or may not have charged upstream)"
                else:
                    heading = f"## Reservation {state}"
                lines = [
                    f"{heading}\n",
                    f"**State**: `{state}`",
                    f"**Nonce**: `{data.get('nonce')}`",
                    f"**Amount**: {format_token_amount(int(data.get('paymentAmountRaw', '0')), usdc_decimals, 'USDC')}",
                ]
                if data.get("txHash"):
                    lines.append(f"**Tx**: `{data['txHash']}`")
                if data.get("settledAt"):
                    lines.append(f"**Settled at**: {data['settledAt']}")
                return "\n".join(lines)

            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                return (
                    f"Timed out after {timeout_seconds}s waiting for reservation `{nonce}` to settle "
                    f"(last state: `{data.get('state')}`). Call this action again to resume waiting."
                )
            _time.sleep(min(interval_seconds, remaining))

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
                status_tag = {"success": "[ok]", "passthrough": "[passthrough]"}.get(tx.get("status", ""), "[failed]")
                lines.append(f"{status_tag} **{tx.get('method', '?')}** {tx.get('targetUrl', '?')}")
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
