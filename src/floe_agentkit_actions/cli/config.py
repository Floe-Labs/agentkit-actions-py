"""Load / save CLI configuration from .floe-agent.json.

v0.4 introduces a per-developer ``agents`` registry. The API key for
each agent is stored in the OS keychain (see ``keychain.py``), never
in this file.

The on-disk file uses **camelCase** keys to match the TypeScript SDK,
so the same ``.floe-agent.json`` is interchangeable between the two
agentkit packages. The Python TypedDict keeps snake_case attribute
names internally; translation happens at the read/write boundary.
Unknown keys (e.g. fields written by a newer CLI) are preserved
verbatim so an older CLI never strips forward-compatible data on save.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict, cast


class AgentRecord(TypedDict, total=False):
    agent_id: int
    name: str
    facilitator_url: str
    privy_wallet_address: str
    key_prefix: str
    created_at: str
    revoked: bool


class FloeAgentConfig(TypedDict, total=False):
    wallet_type: str  # "private-key" | "cdp"
    ai_provider: str  # "openai" | "claude" | "ollama"
    ai_model: str | None
    ollama_base_url: str | None
    rpc_url: str | None
    agents: dict[str, AgentRecord]
    active_agent: str | None


CONFIG_FILE = ".floe-agent.json"

_CONFIG_TO_JSON: dict[str, str] = {
    "wallet_type": "walletType",
    "ai_provider": "aiProvider",
    "ai_model": "aiModel",
    "ollama_base_url": "ollamaBaseUrl",
    "rpc_url": "rpcUrl",
    "agents": "agents",
    "active_agent": "activeAgent",
}
_CONFIG_FROM_JSON: dict[str, str] = {v: k for k, v in _CONFIG_TO_JSON.items()}

_AGENT_TO_JSON: dict[str, str] = {
    "agent_id": "agentId",
    "name": "name",
    "facilitator_url": "facilitatorUrl",
    "privy_wallet_address": "privyWalletAddress",
    "key_prefix": "keyPrefix",
    "created_at": "createdAt",
    "revoked": "revoked",
}
_AGENT_FROM_JSON: dict[str, str] = {v: k for k, v in _AGENT_TO_JSON.items()}


def _config_path() -> Path:
    return Path.cwd() / CONFIG_FILE


def _agent_from_json(raw: dict[str, Any]) -> AgentRecord:
    """Translate a raw on-disk camelCase agent record into snake_case."""
    out: dict[str, Any] = {}
    for k, v in raw.items():
        out[_AGENT_FROM_JSON.get(k, k)] = v
    return cast(AgentRecord, out)


def _agent_to_json(record: AgentRecord) -> dict[str, Any]:
    """Translate an internal snake_case agent record back to camelCase."""
    out: dict[str, Any] = {}
    for k, v in dict(record).items():
        if v is None:
            continue
        out[_AGENT_TO_JSON.get(k, k)] = v
    return out


def load_config() -> FloeAgentConfig | None:
    """Load config from .floe-agent.json in the current directory."""
    path = _config_path()
    try:
        if not path.exists():
            return None
        raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        out: dict[str, Any] = {}
        for key, value in raw.items():
            if key == "agents" and isinstance(value, dict):
                out["agents"] = {
                    name: _agent_from_json(rec) for name, rec in value.items()
                }
            elif key in _CONFIG_FROM_JSON:
                out[_CONFIG_FROM_JSON[key]] = value
            else:
                # Preserve unknown forward-compat keys verbatim so an
                # older CLI never drops fields a newer one wrote.
                out[key] = value
        out.setdefault("wallet_type", "private-key")
        out.setdefault("ai_provider", "openai")
        return cast(FloeAgentConfig, out)
    except Exception:
        return None


def save_config(config: FloeAgentConfig) -> None:
    """Save config to .floe-agent.json (API keys are never cached)."""
    path = _config_path()
    out: dict[str, Any] = {}
    for key, value in dict(config).items():
        if value is None:
            continue
        if key == "agents" and isinstance(value, dict):
            out["agents"] = {
                name: _agent_to_json(rec) for name, rec in value.items()
            }
        elif key in _CONFIG_TO_JSON:
            out[_CONFIG_TO_JSON[key]] = value
        else:
            out[key] = value
    path.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")


def has_config() -> bool:
    return _config_path().exists()


def upsert_agent(config: FloeAgentConfig, record: AgentRecord) -> None:
    """Insert or replace an agent record by name. Mutates ``config`` in place."""
    agents = config.get("agents")
    if not agents:
        agents = {}
        config["agents"] = agents
    agents[record["name"]] = record


def get_agent(config: FloeAgentConfig, name: str) -> AgentRecord | None:
    return (config.get("agents") or {}).get(name)


def list_agents(config: FloeAgentConfig) -> list[AgentRecord]:
    return list((config.get("agents") or {}).values())


def remove_agent(config: FloeAgentConfig, name: str) -> bool:
    agents = config.get("agents") or {}
    if name not in agents:
        return False
    del agents[name]
    if config.get("active_agent") == name:
        config["active_agent"] = None
    return True
