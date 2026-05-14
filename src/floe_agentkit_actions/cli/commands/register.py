"""`floe-agent register --name <name>` — provision a new Floe agent."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from rich.console import Console

from .._prompts import prompt_int, require_prompt
from ..config import (
    FloeAgentConfig,
    load_config_or_exit,
    save_config,
    upsert_agent,
)
from ..floe_api_client import FloeApiClient
from ..keychain import set_agent_key
from ..wallet_factory import create_wallet

console = Console()
USDC_DECIMALS = 6


@dataclass
class RegisterArgs:
    name: str
    facilitator_url: str
    borrow_limit_usdc: str | None = None
    max_rate_bps: int | None = None
    expiry_days: int | None = None
    label: str | None = None


def _usdc_to_raw(amount: str) -> str:
    """Convert a USDC decimal string to a raw-units integer string."""
    try:
        scaled = Decimal(amount) * (Decimal(10) ** USDC_DECIMALS)
    except (InvalidOperation, TypeError, ValueError) as err:
        raise ValueError(f"Invalid USDC amount: {amount}") from err
    if scaled != scaled.to_integral_value():
        raise ValueError(f"USDC amount '{amount}' has more precision than 6 decimals supports.")
    if scaled <= 0:
        raise ValueError(f"USDC amount must be positive, got {amount!r}")
    return str(int(scaled))


def _resolve_wallet_config(existing: FloeAgentConfig | None) -> dict[str, Any]:
    """Build a wallet config dict that ``create_wallet`` accepts."""
    import os

    import questionary

    wallet_type = (existing or {}).get("wallet_type") or "private-key"
    if wallet_type == "private-key":
        pk = os.environ.get("PRIVATE_KEY") or require_prompt(
            questionary.password("Private key (0x...):").ask(), "Private key"
        )
        cfg: dict[str, Any] = {"type": "private-key", "private_key": pk}
        rpc = (existing or {}).get("rpc_url")
        if rpc:
            cfg["rpc_url"] = rpc
        return cfg
    name = os.environ.get("CDP_API_KEY_NAME") or require_prompt(
        questionary.text("CDP API Key Name:").ask(), "CDP API Key Name"
    )
    key = os.environ.get("CDP_API_KEY_PRIVATE_KEY") or require_prompt(
        questionary.password("CDP API Key Private Key:").ask(), "CDP API Key Private Key"
    )
    return {"type": "cdp", "api_key_name": name, "api_key_private_key": key}


def run_register_command(args: RegisterArgs) -> None:
    existing = load_config_or_exit()
    if existing and (existing.get("agents") or {}).get(args.name):
        console.print(
            f"[red]An agent named \"{args.name}\" already exists in local config. "
            f"Pick a different name or remove it from .floe-agent.json.[/red]"
        )
        sys.exit(1)

    config: FloeAgentConfig = existing or FloeAgentConfig(
        wallet_type="private-key",
        ai_provider="openai",
    )

    import questionary

    borrow_limit_usdc = args.borrow_limit_usdc or require_prompt(
        questionary.text("Borrow limit (USDC, e.g. 10000):", default="10000").ask(),
        "Borrow limit",
    )
    max_rate_bps = (
        args.max_rate_bps
        if args.max_rate_bps is not None
        else prompt_int("Max interest rate (bps, e.g. 1500 = 15%):", "1500")
    )
    expiry_days = (
        args.expiry_days
        if args.expiry_days is not None
        else prompt_int("Delegation expiry (days):", "90")
    )

    if not (1 <= max_rate_bps <= 10000):
        console.print("[red]max_rate_bps must be between 1 and 10000[/red]")
        sys.exit(1)
    if not (1 <= expiry_days <= 3650):
        console.print("[red]expiry_days must be between 1 and 3650[/red]")
        sys.exit(1)

    try:
        borrow_limit_raw = _usdc_to_raw(borrow_limit_usdc)
    except ValueError as err:
        console.print(f"[red]{err}[/red]")
        sys.exit(1)

    wallet_config = _resolve_wallet_config(existing)
    # Persist the wallet type the user actually picked. Without this, a
    # first-time `register` would carry `wallet_type='private-key'` forward
    # even if the user chose CDP, so later `run`/`rotate`/`revoke` calls
    # would silently prompt for the wrong wallet kind.
    config["wallet_type"] = wallet_config["type"]
    wallet_provider = create_wallet(wallet_config)
    client = FloeApiClient(args.facilitator_url, wallet_provider)

    console.print(f"[dim]Registering agent \"{args.name}\"...[/dim]")
    try:
        created = client.create_agent({
            "name": args.name,
            "borrow_limit_raw": borrow_limit_raw,
            "max_rate_bps": max_rate_bps,
            "expiry_seconds": expiry_days * 86400,
        })
    except Exception as err:  # noqa: BLE001
        console.print(f"[red]Registration failed: {err}[/red]")
        sys.exit(1)
    console.print(
        f"[green]Agent \"{args.name}\" created "
        f"(id={created['agent_id']}, status={created['status']})[/green]"
    )

    console.print("[dim]Minting API key...[/dim]")
    try:
        key_response = client.create_agent_key(created["agent_id"], label=args.label or args.name)
    except Exception as err:  # noqa: BLE001
        # Persist the partial agent record BEFORE telling the user to rotate.
        # Without this save the rotate command can't find the agent locally
        # and the recovery instruction would dead-end.
        from datetime import datetime, timezone

        upsert_agent(
            config,
            {
                "agent_id": created["agent_id"],
                "name": args.name,
                "facilitator_url": args.facilitator_url,
                "privy_wallet_address": created["privy_wallet_address"],
                "key_prefix": "",
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        config["active_agent"] = args.name
        save_config(config)
        console.print(
            f"[red]Key minting failed: {err}[/red]\n"
            f"[yellow]  The agent was created (id={created['agent_id']}) and saved to "
            f".floe-agent.json. Run `floe-agent rotate {args.name}` to recover.[/yellow]"
        )
        sys.exit(1)
    console.print("[green]API key minted[/green]")

    upsert_agent(
        config,
        {
            "agent_id": created["agent_id"],
            "name": args.name,
            "facilitator_url": args.facilitator_url,
            "privy_wallet_address": created["privy_wallet_address"],
            "key_prefix": key_response["key_prefix"],
            "created_at": key_response["created_at"],
        },
    )
    config["active_agent"] = args.name
    save_config(config)

    # Defensive: keychain write failures must not swallow the one-time key.
    # set_agent_key already handles "no backend" via env-var fallback, but
    # set_password can still throw in locked / DBus-offline environments.
    try:
        stored = set_agent_key(args.name, args.facilitator_url, key_response["key"])
    except Exception as err:  # noqa: BLE001
        console.print(
            f"[yellow]  Keychain write failed: {err}. "
            "Capture the key shown below — it won't be regenerated.[/yellow]"
        )
        stored = "env-fallback"

    console.print("")
    console.print(f"[bold]  Agent \"{args.name}\" is ready.[/bold]\n")
    console.print(
        f"  [bold]API Key:[/bold] [yellow]{key_response['key']}[/yellow] "
        "[dim](shown ONCE)[/dim]"
    )
    if stored == "keychain":
        console.print(
            f"[dim]  Saved to OS keychain — load it via `floe-agent run --agent {args.name}`.[/dim]"
        )
    else:
        from ..keychain import env_var_name_for

        console.print(
            f"[yellow]  OS keychain unavailable. Export "
            f"{env_var_name_for(args.name, args.facilitator_url)} "
            "to use this key later.[/yellow]"
        )
    console.print("")
