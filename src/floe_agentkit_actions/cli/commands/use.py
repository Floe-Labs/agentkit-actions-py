"""`floe-agent use <name>` — set the active agent."""

from __future__ import annotations

import sys

from rich.console import Console

from ..config import get_agent, load_config_or_exit, save_config

console = Console()


def run_use_command(name: str) -> None:
    config = load_config_or_exit()
    if config is None:
        console.print("[red]No config found. Register an agent first.[/red]")
        sys.exit(1)
    if get_agent(config, name) is None:
        console.print(f"[red]Unknown agent \"{name}\". Run `floe-agent agents` to list.[/red]")
        sys.exit(1)
    config["active_agent"] = name
    save_config(config)
    console.print(f"[green]  Active agent set to \"{name}\".[/green]")
