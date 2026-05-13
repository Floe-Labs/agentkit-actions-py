"""Minimal HTTP client for the Floe Developer API.

Wraps the wallet-signature auth flow so subcommand modules can stay
focused on UX. Auth headers are rebuilt per request — timestamps drift,
so cached headers wouldn't survive past the 5-minute window anyway.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, TypedDict
from urllib.parse import urlparse

from .signature_auth import build_auth_headers

FETCH_TIMEOUT_SECONDS = 30


class CreateAgentInput(TypedDict):
    name: str
    borrow_limit_raw: str
    max_rate_bps: int
    expiry_seconds: int


class CreateAgentResponse(TypedDict):
    agent_id: int
    status: str
    privy_wallet_address: str
    delegation_tx_hash: str


class CreateKeyResponse(TypedDict):
    key: str
    id: int
    key_prefix: str
    label: str | None
    permissions: str
    created_at: str


class ListedKey(TypedDict):
    id: int
    key_prefix: str
    label: str | None
    permissions: str
    last_used_at: str | None
    created_at: str


def _request_json(
    url: str,
    *,
    method: str,
    headers: dict[str, str],
    body: Any = None,
) -> tuple[int, Any]:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Invalid URL scheme: {parsed.scheme}")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as err:
        raw = err.read().decode() if err.fp else ""
        try:
            return err.code, json.loads(raw)
        except json.JSONDecodeError:
            return err.code, {"error": raw or err.reason}


def _read_error(payload: Any) -> str:
    if isinstance(payload, dict):
        return payload.get("detail") or payload.get("error") or payload.get("message") or "unknown error"
    return str(payload)


class FloeApiClient:
    def __init__(self, facilitator_url: str, wallet_provider: Any) -> None:
        self.base_url = facilitator_url.rstrip("/")
        self.wallet_provider = wallet_provider

    def create_agent(self, payload: CreateAgentInput) -> CreateAgentResponse:
        body = {
            "name": payload["name"],
            "borrowLimitRaw": payload["borrow_limit_raw"],
            "maxRateBps": payload["max_rate_bps"],
            "expirySeconds": payload["expiry_seconds"],
        }
        status, data = _request_json(
            f"{self.base_url}/v1/developer/agents",
            method="POST",
            headers=build_auth_headers(self.wallet_provider),
            body=body,
        )
        if status >= 400:
            raise RuntimeError(f"createAgent: {status} {_read_error(data)}")
        return CreateAgentResponse(
            agent_id=int(data["agentId"]),
            status=str(data.get("status", "")),
            privy_wallet_address=str(data.get("privyWalletAddress", "")),
            delegation_tx_hash=str(data.get("delegationTxHash", "")),
        )

    def create_agent_key(
        self,
        agent_id: int,
        label: str | None = None,
        permissions: str | None = None,
    ) -> CreateKeyResponse:
        body: dict[str, Any] = {}
        if label is not None:
            body["label"] = label
        if permissions is not None:
            body["permissions"] = permissions
        status, data = _request_json(
            f"{self.base_url}/v1/developer/agents/{agent_id}/keys",
            method="POST",
            headers=build_auth_headers(self.wallet_provider),
            body=body,
        )
        if status >= 400:
            raise RuntimeError(f"createAgentKey: {status} {_read_error(data)}")
        return _coerce_key_response(data)

    def list_agent_keys(self, agent_id: int) -> list[ListedKey]:
        status, data = _request_json(
            f"{self.base_url}/v1/developer/agents/{agent_id}/keys",
            method="GET",
            headers=build_auth_headers(self.wallet_provider),
        )
        if status >= 400:
            raise RuntimeError(f"listAgentKeys: {status} {_read_error(data)}")
        keys = (data or {}).get("keys", [])
        return [
            ListedKey(
                id=int(k["id"]),
                key_prefix=str(k.get("keyPrefix", "")),
                label=k.get("label"),
                permissions=str(k.get("permissions", "")),
                last_used_at=k.get("lastUsedAt"),
                created_at=str(k.get("createdAt", "")),
            )
            for k in keys
        ]

    def revoke_agent_key(self, agent_id: int, key_id: int) -> None:
        status, data = _request_json(
            f"{self.base_url}/v1/developer/agents/{agent_id}/keys/{key_id}",
            method="DELETE",
            headers=build_auth_headers(self.wallet_provider),
        )
        if status >= 400:
            raise RuntimeError(f"revokeAgentKey: {status} {_read_error(data)}")

    def rotate_agent_key(self, agent_id: int, key_id: int) -> CreateKeyResponse:
        status, data = _request_json(
            f"{self.base_url}/v1/developer/agents/{agent_id}/keys/{key_id}/rotate",
            method="POST",
            headers=build_auth_headers(self.wallet_provider),
            body={},
        )
        if status >= 400:
            raise RuntimeError(f"rotateAgentKey: {status} {_read_error(data)}")
        return _coerce_key_response(data)

    def open_credit_line(
        self,
        agent_id: int,
        deposit_raw: str,
        max_ltv_bps: int | None = None,
        max_rate_bps: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"depositRaw": deposit_raw}
        if max_ltv_bps is not None:
            body["maxLtvBps"] = max_ltv_bps
        if max_rate_bps is not None:
            body["maxRateBps"] = max_rate_bps
        status, data = _request_json(
            f"{self.base_url}/v1/developer/agents/{agent_id}/open-credit-line",
            method="POST",
            headers=build_auth_headers(self.wallet_provider),
            body=body,
        )
        if status >= 400:
            raise RuntimeError(f"openCreditLine: {status} {_read_error(data)}")
        return data or {}


def _coerce_key_response(data: dict[str, Any]) -> CreateKeyResponse:
    return CreateKeyResponse(
        key=str(data["key"]),
        id=int(data["id"]),
        key_prefix=str(data.get("keyPrefix", "")),
        label=data.get("label"),
        permissions=str(data.get("permissions", "")),
        created_at=str(data.get("createdAt", "")),
    )
