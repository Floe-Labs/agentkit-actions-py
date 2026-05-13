"""`floe-agent revoke <name>` — revoke the agent's API key on-server + locally."""

from __future__ import annotations

import sys
from typing import Any

from rich.console import Console

from ..config import FloeAgentConfig, get_agent, load_config, save_config
from ..floe_api_client import FloeApiClient
from ..keychain import delete_agent_key
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


def run_revoke_command(name: str, facilitator_url: str) -> None:
    config = load_config()
    if config is None:
        console.print("[red]No config found.[/red]")
        sys.exit(1)
    agent = get_agent(config, name)
    if agent is None:
        console.print(f"[red]Unknown agent \"{name}\".[/red]")
        sys.exit(1)

    import questionary

    confirmed = questionary.confirm(
        f"Revoke API key for \"{name}\" (id={agent['agent_id']})? This cannot be undone.",
        default=False,
    ).ask()
    if not confirmed:
        console.print("[dim]Aborted.[/dim]")
        return

    wallet_provider = create_wallet(_resolve_wallet_config(config))
    client = FloeApiClient(facilitator_url, wallet_provider)

    console.print("[dim]Looking up active key...[/dim]")
    try:
        keys = client.list_agent_keys(agent["agent_id"])
    except Exception as err:  # noqa: BLE001
        console.print(f"[red]Listing keys failed: {err}[/red]")
        sys.exit(1)

    if not keys:
        console.print("[yellow]  No active keys server-side. Clearing local entry.[/yellow]")
    else:
        try:
            client.revoke_agent_key(agent["agent_id"], keys[0]["id"])
            console.print(f"[green]  Revoked key {keys[0]['key_prefix']}[/green]")
        except Exception as err:  # noqa: BLE001
            console.print(f"[red]Revoke failed: {err}[/red]")
            sys.exit(1)

    delete_agent_key(name, agent["facilitator_url"])
    agent["revoked"] = True
    save_config(config)
    console.print(f"[green]  Local keychain entry for \"{name}\" removed.[/green]")
