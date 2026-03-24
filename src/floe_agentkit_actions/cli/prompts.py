"""Interactive setup flow using questionary."""

from __future__ import annotations

import os
from typing import Any

import questionary
from rich.console import Console

from .ai_factory import validate_ollama_connection
from .config import FloeAgentConfig

console = Console()


def run_setup_flow(saved_config: FloeAgentConfig | None = None) -> dict[str, Any]:
    """Run the interactive setup flow and return wallet_config + ai_config + rpc_url.

    Returns dict with keys: wallet_config, ai_config, rpc_url
    """
    # Step 1: Wallet type
    wallet_type = (saved_config or {}).get("wallet_type") or _prompt_wallet_type()

    # Step 2: Wallet credentials (always prompt — never cached)
    wallet_config: dict[str, Any]
    if wallet_type == "private-key":
        pk = os.environ.get("PRIVATE_KEY")
        if pk:
            console.print("[dim]  Using PRIVATE_KEY from environment[/dim]")
            wallet_config = {"type": "private-key", "private_key": pk}
        else:
            key = questionary.password("Private key (0x...):").ask()
            wallet_config = {"type": "private-key", "private_key": key}
    else:
        name = os.environ.get("CDP_API_KEY_NAME")
        key = os.environ.get("CDP_API_KEY_PRIVATE_KEY")
        if name and key:
            console.print("[dim]  Using CDP credentials from environment[/dim]")
            wallet_config = {"type": "cdp", "api_key_name": name, "api_key_private_key": key}
        else:
            api_key_name = name or questionary.text("CDP API Key Name:").ask()
            api_key_private_key = key or questionary.password("CDP API Key Private Key:").ask()
            wallet_config = {
                "type": "cdp",
                "api_key_name": api_key_name,
                "api_key_private_key": api_key_private_key,
            }

    # Step 3: RPC URL
    rpc_url: str | None = None
    env_rpc = os.environ.get("BASE_RPC_URL")
    if env_rpc:
        console.print("[dim]  Using BASE_RPC_URL from environment[/dim]")
        rpc_url = env_rpc
    elif saved_config and saved_config.get("rpc_url"):
        rpc_url = saved_config["rpc_url"]
    else:
        rpc_url = questionary.text(
            "Base Mainnet RPC URL (leave blank for public):", default=""
        ).ask()
        if not rpc_url:
            rpc_url = None

    if wallet_config["type"] == "private-key" and rpc_url:
        wallet_config["rpc_url"] = rpc_url

    # Step 4: AI provider
    ai_provider = (saved_config or {}).get("ai_provider") or _prompt_ai_provider()

    # Step 5: API key (always prompt — never cached)
    api_key: str | None = None
    if ai_provider == "openai":
        env_key = os.environ.get("OPENAI_API_KEY")
        if env_key:
            console.print("[dim]  Using OPENAI_API_KEY from environment[/dim]")
            api_key = env_key
        else:
            api_key = questionary.password("OpenAI API Key:").ask()
    elif ai_provider == "claude":
        env_key = os.environ.get("ANTHROPIC_API_KEY")
        if env_key:
            console.print("[dim]  Using ANTHROPIC_API_KEY from environment[/dim]")
            api_key = env_key
        else:
            api_key = questionary.password("Anthropic API Key:").ask()

    # Step 6: Ollama-specific config
    ollama_base_url: str | None = None
    ai_model = (saved_config or {}).get("ai_model")
    if ai_provider == "ollama":
        ollama_base_url = (saved_config or {}).get("ollama_base_url") or questionary.text(
            "Ollama base URL:", default="http://localhost:11434/api"
        ).ask()
        ai_model = ai_model or questionary.text("Model name:", default="llama3.1").ask()

        connected = validate_ollama_connection(ollama_base_url or "http://localhost:11434/api")
        if not connected:
            console.print(
                f"[yellow]\n  Warning: Cannot connect to Ollama at {ollama_base_url}[/yellow]"
            )
            console.print("[yellow]  Make sure Ollama is running: ollama serve\n[/yellow]")
        console.print(
            "[yellow]  Note: Local models may struggle with complex multi-step tool chains.\n[/yellow]"
        )

    ai_config: dict[str, Any] = {
        "provider": ai_provider,
        "api_key": api_key,
        "model": ai_model,
        "ollama_base_url": ollama_base_url,
    }

    return {
        "wallet_config": wallet_config,
        "ai_config": ai_config,
        "rpc_url": rpc_url,
    }


def prompt_reuse_saved_config(config: FloeAgentConfig) -> bool:
    console.print(
        f"[dim]  Found saved config: {config.get('wallet_type', 'unknown')} wallet"
        f" + {config.get('ai_provider', 'unknown')}[/dim]"
    )
    return questionary.confirm("Use saved configuration?", default=True).ask() or False


def _prompt_wallet_type() -> str:
    return (
        questionary.select(
            "Select wallet provider:",
            choices=[
                questionary.Choice("Private Key (direct key - you pay gas)", value="private-key"),
                questionary.Choice("CDP Wallet (Coinbase MPC - no raw key exposure)", value="cdp"),
            ],
        ).ask()
        or "private-key"
    )


def _prompt_ai_provider() -> str:
    return (
        questionary.select(
            "Select AI provider:",
            choices=[
                questionary.Choice("OpenAI (GPT-4o)", value="openai"),
                questionary.Choice("Claude (Anthropic)", value="claude"),
                questionary.Choice(
                    "Ollama (Local - free, requires ollama running)", value="ollama"
                ),
            ],
        ).ask()
        or "openai"
    )
