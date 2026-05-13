"""`floe-agent open-credit-line --name <name> --deposit <usdc>` — open a credit line."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from rich.console import Console

from ..config import FloeAgentConfig, get_agent, load_config
from ..floe_api_client import FloeApiClient
from ..wallet_factory import create_wallet

console = Console()
USDC_DECIMALS = 6


@dataclass
class OpenCreditLineArgs:
    name: str
    deposit_usdc: str | None = None
    max_ltv_bps: int | None = None
    max_rate_bps: int | None = None


def _usdc_to_raw(amount: str) -> str:
    try:
        scaled = Decimal(amount) * (Decimal(10) ** USDC_DECIMALS)
    except (InvalidOperation, TypeError, ValueError) as err:
        raise ValueError(f"Invalid USDC amount: {amount}") from err
    if scaled != scaled.to_integral_value():
        raise ValueError(f"USDC amount '{amount}' has more precision than {USDC_DECIMALS} decimals supports.")
    if scaled <= 0:
        raise ValueError(f"USDC amount must be positive, got {amount!r}")
    return str(int(scaled))


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


def run_open_credit_line_command(args: OpenCreditLineArgs) -> None:
    config = load_config()
    if config is None:
        console.print("[red]No config found. Register an agent first with `floe-agent register`.[/red]")
        sys.exit(1)
    agent = get_agent(config, args.name)
    if agent is None:
        console.print(
            f"[red]Unknown agent \"{args.name}\". Run `floe-agent agents` to list.[/red]"
        )
        sys.exit(1)

    import questionary

    deposit_usdc = args.deposit_usdc or questionary.text(
        "Deposit (USDC, will be locked as collateral, e.g. 10000):", default="10000"
    ).ask()

    try:
        deposit_raw = _usdc_to_raw(deposit_usdc)
    except ValueError as err:
        console.print(f"[red]{err}[/red]")
        sys.exit(1)

    if args.max_ltv_bps is not None and not (1 <= args.max_ltv_bps <= 9500):
        console.print("[red]max_ltv_bps must be 1..9500 (95% is the USDC/USDC market cap).[/red]")
        sys.exit(1)
    if args.max_rate_bps is not None and not (1 <= args.max_rate_bps <= 10000):
        console.print("[red]max_rate_bps must be 1..10000.[/red]")
        sys.exit(1)

    wallet_provider = create_wallet(_resolve_wallet_config(config))
    client = FloeApiClient(agent["facilitator_url"], wallet_provider)

    console.print(f"[dim]Opening credit line for \"{args.name}\"...[/dim]")
    try:
        result = client.open_credit_line(
            agent["agent_id"],
            deposit_raw=deposit_raw,
            max_ltv_bps=args.max_ltv_bps,
            max_rate_bps=args.max_rate_bps,
        )
    except Exception as err:  # noqa: BLE001
        console.print(f"[red]Open credit line failed: {err}[/red]")
        sys.exit(1)

    console.print(
        f"[green]Borrow intent posted "
        f"(loanId={result.get('loanId')}, principal={result.get('principalRaw')} raw USDC)[/green]"
    )
    console.print("")
    console.print(f"[bold]  Credit line submitted for \"{args.name}\".[/bold]\n")
    if result.get("approveTxHash"):
        console.print(f"  [bold]Approve tx:[/bold] {result['approveTxHash']}")
    console.print(f"  [bold]Register tx:[/bold] {result.get('registerTxHash')}")
    console.print(f"  [bold]Deposit:[/bold] {deposit_raw} raw USDC")
    console.print(f"  [bold]Borrow:[/bold] {result.get('principalRaw')} raw USDC")
    console.print("")
    console.print(
        "[dim]  Status is pending_on_chain. The reconciler advances to pending_match once the receipt confirms,\n"
        "  then the solver matches the intent and status flips to active. At that point your agent's\n"
        "  /proxy/fetch calls will succeed.[/dim]"
    )
    console.print("")
