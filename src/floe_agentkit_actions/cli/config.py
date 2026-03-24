"""Load / save CLI configuration from .floe-agent.json."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict


class FloeAgentConfig(TypedDict, total=False):
    wallet_type: str  # "private-key" | "cdp"
    ai_provider: str  # "openai" | "claude" | "ollama"
    ai_model: str | None
    ollama_base_url: str | None
    rpc_url: str | None


CONFIG_FILE = ".floe-agent.json"


def _config_path() -> Path:
    return Path.cwd() / CONFIG_FILE


def load_config() -> FloeAgentConfig | None:
    """Load config from .floe-agent.json in the current directory."""
    path = _config_path()
    try:
        if not path.exists():
            return None
        data: dict[str, Any] = json.loads(path.read_text())
        return FloeAgentConfig(
            wallet_type=data.get("wallet_type", "private-key"),
            ai_provider=data.get("ai_provider", "openai"),
            ai_model=data.get("ai_model"),
            ollama_base_url=data.get("ollama_base_url"),
            rpc_url=data.get("rpc_url"),
        )
    except Exception:
        return None


def save_config(config: FloeAgentConfig) -> None:
    """Save config to .floe-agent.json (API keys are never cached)."""
    path = _config_path()
    path.write_text(json.dumps(dict(config), indent=2) + "\n")


def has_config() -> bool:
    return _config_path().exists()
