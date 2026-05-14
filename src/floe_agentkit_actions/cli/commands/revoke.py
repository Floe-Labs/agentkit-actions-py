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
    # Hit the facilitator the agent was actually registered against; the
    # caller-supplied `facilitator_url` (default or --flag) is only a
    # fallback when local state is missing. Otherwise revoke could 401
    # against a different backend that has never seen this key.
    client = FloeApiClient(agent.get("facilitator_url") or facilitator_url, wallet_provider)

    console.print("[dim]Looking up active key...[/dim]")
    try:
        keys = client.list_agent_keys(agent["agent_id"])
    except Exception as err:  # noqa: BLE001
        console.print(f"[red]Listing keys failed: {err}[/red]")
        sys.exit(1)

    # Prefer the key matching the locally tracked prefix; fall back to
    # keys[0] so the cap-of-1 case still works if local state drifted.
    target = None
    if keys:
        if agent.get("key_prefix"):
            target = next((k for k in keys if k.get("key_prefix") == agent["key_prefix"]), None)
        if target is None:
            target = keys[0]

    if target is None:
        console.print("[yellow]  No active keys server-side. Clearing local entry.[/yellow]")
    else:
        try:
            client.revoke_agent_key(agent["agent_id"], target["id"])
            console.print(f"[green]  Revoked key {target['key_prefix']}[/green]")
        except Exception as err:  # noqa: BLE001
            console.print(f"[red]Revoke failed: {err}[/red]")
            sys.exit(1)

    deleted = delete_agent_key(name, agent["facilitator_url"])
    if deleted:
        agent["revoked"] = True
        save_config(config)
        console.print(f"[green]  Local keychain entry for \"{name}\" removed.[/green]")
    else:
        # Server-side revoke already succeeded; local cleanup is the only
        # thing left. Don't claim success — surface the partial state so
        # the user knows to clear the env-var fallback or rerun manually.
        console.print(
            "[yellow]  Local keychain entry could not be deleted "
            "(no keyring backend or already absent). "
            "If a FLOE_AGENT_KEY_* env var is set, unset it manually.[/yellow]"
        )
