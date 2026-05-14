"""`floe-agent rotate <name>` — atomically rotate the agent's API key."""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console

from ..config import FloeAgentConfig, get_agent, load_config, save_config, upsert_agent
from ..floe_api_client import FloeApiClient
from ..keychain import set_agent_key
from ..wallet_factory import create_wallet

console = Console()


def _resolve_wallet_config(existing: FloeAgentConfig) -> dict[str, Any]:
    import os

    import questionary

    if (existing.get("wallet_type") or "private-key") == "private-key":
        pk = os.environ.get("PRIVATE_KEY") or questionary.password("Private key (0x...):").ask()
        cfg: dict[str, Any] = {"type": "private-key", "private_key": pk}
        if existing.get("rpc_url"):
            cfg["rpc_url"] = existing["rpc_url"]
        return cfg
    name = os.environ.get("CDP_API_KEY_NAME") or questionary.text("CDP API Key Name:").ask()
    key = (
        os.environ.get("CDP_API_KEY_PRIVATE_KEY")
        or questionary.password("CDP API Key Private Key:").ask()
    )
    return {"type": "cdp", "api_key_name": name, "api_key_private_key": key}


def run_rotate_command(name: str) -> None:
    config = load_config()
    if config is None:
        console.print("[red]No config found.[/red]")
        sys.exit(1)
    agent = get_agent(config, name)
    if agent is None:
        console.print(f"[red]Unknown agent \"{name}\".[/red]")
        sys.exit(1)

    wallet_provider = create_wallet(_resolve_wallet_config(config))
    client = FloeApiClient(agent["facilitator_url"], wallet_provider)

    console.print("[dim]Rotating API key...[/dim]")
    try:
        keys = client.list_agent_keys(agent["agent_id"])
        if not keys:
            console.print("[red]No active key to rotate. Use `floe-agent register` instead.[/red]")
            sys.exit(1)
        # Prefer the key matching the locally tracked prefix; fall back to
        # keys[0] so the cap-of-1 case still works when local state has
        # drifted (e.g., user rotated through the dashboard).
        current = None
        if agent.get("key_prefix"):
            current = next((k for k in keys if k.get("key_prefix") == agent["key_prefix"]), None)
        if current is None:
            current = keys[0]
        rotated = client.rotate_agent_key(agent["agent_id"], current["id"])
    except Exception as err:  # noqa: BLE001
        console.print(f"[red]Rotate failed: {err}[/red]")
        sys.exit(1)
    console.print(
        f"[green]Rotated key (old: {current['key_prefix']}, new: {rotated['key_prefix']})[/green]"
    )

    agent["key_prefix"] = rotated["key_prefix"]
    agent["revoked"] = False
    upsert_agent(config, agent)
    save_config(config)

    # Defensive: keychain write failures must not swallow the one-time key.
    stored_in_keychain = True
    try:
        set_agent_key(name, agent["facilitator_url"], rotated["key"])
    except Exception as err:  # noqa: BLE001
        stored_in_keychain = False
        console.print(
            f"[yellow]  Keychain write failed: {err}. "
            "Capture the key shown below — it won't be regenerated.[/yellow]"
        )

    console.print("")
    console.print(
        f"  [bold]New API Key:[/bold] [yellow]{rotated['key']}[/yellow] "
        "[dim](shown ONCE)[/dim]"
    )
    if stored_in_keychain:
        console.print("[dim]  Stored in OS keychain (or env-var fallback).[/dim]\n")
    else:
        from ..keychain import env_var_name_for

        env_name = env_var_name_for(name, agent["facilitator_url"])
        console.print(
            f"[dim]  Export {env_name} to load this key on next `floe-agent run`.[/dim]\n"
        )
