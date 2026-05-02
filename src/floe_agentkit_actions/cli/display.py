"""Terminal display helpers: banner, session info, help text."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel

console = Console()


def print_banner() -> None:
    console.print(
        Panel(
            "[bold cyan]Floe Agent CLI[/bold cyan]\n[dim]DeFi Lending on Base Mainnet[/dim]",
            expand=False,
            border_style="cyan",
        )
    )
    console.print()


def print_session_info(
    address: str,
    wallet_type: str,
    ai_provider: str,
    ai_model: str,
    tool_count: int,
) -> None:
    console.print("[dim]" + "\u2500" * 50 + "[/dim]")
    console.print(f"  [bold]Wallet:[/bold]     {address}")
    console.print(f"  [bold]Type:[/bold]       {wallet_type}")
    console.print("  [bold]Network:[/bold]    Base Mainnet")
    console.print(f"  [bold]AI:[/bold]         {ai_provider} ({ai_model})")
    console.print(f"  [bold]Tools:[/bold]      {tool_count} actions available")
    console.print("[dim]" + "\u2500" * 50 + "[/dim]")
    console.print()


def print_help() -> None:
    console.print(
        """
  [bold]Commands:[/bold]
    [cyan]exit[/cyan]     Quit the agent
    [cyan]help[/cyan]     Show this help message
    [cyan]wallet[/cyan]   Show wallet info
    [cyan]clear[/cyan]    Clear conversation history
    [cyan]config[/cyan]   Show current configuration
    [cyan]save[/cyan]     Save current config for next time

  [bold]Tips:[/bold]
    - Ask about markets, loans, prices, or intents
    - The AI will confirm before executing write operations
    - Transaction hashes link to BaseScan
"""
    )
