"""`floe-agent agents` — list registered agents and their key status."""

from __future__ import annotations

from rich.console import Console

from ..config import list_agents, load_config_or_exit
from ..keychain import get_agent_key

console = Console()


def run_list_command() -> None:
    config = load_config_or_exit()
    if config is None:
        console.print("[dim]No config found. Run `floe-agent register --name <name>` first.[/dim]")
        return
    agents = list_agents(config)
    if not agents:
        console.print("[dim]No agents registered.[/dim]")
        return

    active = config.get("active_agent")
    console.print("")
    console.print("[bold]  Registered Floe agents:[/bold]\n")
    for a in agents:
        key = get_agent_key(a["name"], a["facilitator_url"])
        if a.get("revoked"):
            key_status = "[dim]revoked[/dim]"
        elif key:
            key_status = "[green]key present[/green]"
        else:
            key_status = "[yellow]key MISSING[/yellow]"
        marker = "[green]● [/green]" if a["name"] == active else "  "
        console.print(
            f"{marker}[bold]{a['name']}[/bold]  "
            f"[dim](id={a['agent_id']}, {a.get('key_prefix', '')}…)[/dim]  {key_status}"
        )
        console.print(f"    [dim]privy: {a['privy_wallet_address']}[/dim]")
        console.print(f"    [dim]facilitator: {a['facilitator_url']}[/dim]")
    console.print("")
