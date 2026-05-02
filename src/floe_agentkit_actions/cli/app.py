"""Main CLI application — interactive REPL loop with AI chat."""

from __future__ import annotations

import json
import signal
import sys
from typing import Any

from rich.console import Console

from ..constants import BASE_MAINNET_MATCHER, LENDING_MATCHER_ABI
from .ai_factory import AIClient, _default_model, create_ai_client
from .config import FloeAgentConfig, load_config, save_config
from .display import print_banner, print_help, print_session_info
from .prompts import prompt_reuse_saved_config, run_setup_flow
from .wallet_factory import create_wallet

console = Console()
VERSION = "0.1.0"

# Known market pairs on Base Mainnet
MARKET_PAIRS: list[dict[str, str]] = [
    {
        "loan": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "collateral": "0x4200000000000000000000000000000000000006",
        "label": "USDC/WETH",
    },
    {
        "loan": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "collateral": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        "label": "USDC/cbBTC",
    },
    {
        "loan": "0xfde4C96c8593536E31F229EA8f37b2ADa2699bb2",
        "collateral": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        "label": "USDT/cbBTC",
    },
]


def run() -> None:
    """Entry point for the CLI."""
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print("Usage: floe-agent [options]\n")
        print("Interactive DeFi agent for the Floe lending protocol on Base Mainnet.\n")
        print("Options:")
        print("  --help, -h       Show this help message")
        print("  --version, -v    Show version")
        print("\nEnvironment variables:")
        print("  PRIVATE_KEY              Wallet private key (0x...)")
        print("  CDP_API_KEY_NAME         Coinbase CDP API key name")
        print("  CDP_API_KEY_PRIVATE_KEY  Coinbase CDP API private key")
        print("  OPENAI_API_KEY           OpenAI API key")
        print("  ANTHROPIC_API_KEY        Anthropic API key")
        print("  BASE_RPC_URL             Custom Base Mainnet RPC (recommended)")
        sys.exit(0)

    if "--version" in args or "-v" in args:
        print(VERSION)
        sys.exit(0)

    print_banner()

    # Check for saved config
    saved_config = load_config()
    if saved_config and prompt_reuse_saved_config(saved_config):
        setup_result = run_setup_flow(saved_config)
    else:
        setup_result = run_setup_flow()

    wallet_config = setup_result["wallet_config"]
    ai_config = setup_result["ai_config"]

    # Create wallet
    console.print("[dim]Creating wallet...[/dim]")
    try:
        wallet_provider = create_wallet(wallet_config)
        console.print("[green]Wallet connected[/green]")
    except Exception as e:
        console.print(f"[red]Wallet creation failed: {e}[/red]")
        sys.exit(1)

    # Create AI model
    console.print("[dim]Connecting to AI provider...[/dim]")
    try:
        ai_client = create_ai_client(ai_config)
        console.print("[green]AI provider connected[/green]")
    except Exception as e:
        console.print(f"[red]AI connection failed: {e}[/red]")
        sys.exit(1)

    # Discover markets
    console.print("[dim]Discovering markets...[/dim]")
    known_market_ids = _discover_market_ids(wallet_provider)
    console.print(f"[green]Found {len(known_market_ids)} markets[/green]")

    # Initialize Floe actions
    console.print("[dim]Initializing Floe actions...[/dim]")
    from .. import floe_action_provider
    from ..types import FloeConfig

    provider = floe_action_provider(FloeConfig(known_market_ids=known_market_ids))

    # Build tool definitions for AI
    tools, action_map = _build_tools(provider, wallet_provider)
    console.print(f"[green]{len(tools)} Floe actions loaded[/green]")

    # Display session info
    address = wallet_provider.get_address()
    print_session_info(
        address=address,
        wallet_type="CDP (MPC)" if wallet_config["type"] == "cdp" else "Private Key",
        ai_provider=ai_config["provider"],
        ai_model=ai_config.get("model") or _default_model(ai_config["provider"]),
        tool_count=len(tools),
    )

    console.print('[dim]Type "help" for commands or start chatting.\n[/dim]')

    system_prompt = _build_system_prompt(address, [t["function"]["name"] for t in tools])

    # Current config for save command
    current_config = FloeAgentConfig(
        wallet_type=wallet_config["type"],
        ai_provider=ai_config["provider"],
        ai_model=ai_config.get("model"),
        ollama_base_url=ai_config.get("ollama_base_url"),
        rpc_url=setup_result.get("rpc_url"),
    )

    messages: list[dict[str, Any]] = []

    # Graceful shutdown
    def _sigint_handler(_sig: int, _frame: Any) -> None:
        console.print("\n\n[dim]Goodbye![/dim]")
        sys.exit(0)

    signal.signal(signal.SIGINT, _sigint_handler)

    # Main chat loop
    while True:
        try:
            user_input = console.input("[cyan]You: [/cyan]")
        except EOFError:
            break

        trimmed = user_input.strip()
        if not trimmed:
            continue

        cmd = trimmed.lower()
        if cmd in ("exit", "quit"):
            console.print("[dim]Goodbye![/dim]")
            break
        if cmd == "help":
            print_help()
            continue
        if cmd == "wallet":
            console.print(f"\n  [bold]Address:[/bold] {address}")
            console.print("  [bold]Network:[/bold] Base Mainnet\n")
            continue
        if cmd == "clear":
            messages.clear()
            console.clear()
            print_banner()
            console.print("[dim]Conversation cleared.\n[/dim]")
            continue
        if cmd == "config":
            console.print(f"\n  [bold]Wallet:[/bold] {current_config.get('wallet_type')}")
            model = current_config.get("ai_model") or _default_model(
                current_config.get("ai_provider", "openai")
            )
            console.print(f"  [bold]AI:[/bold] {current_config.get('ai_provider')} ({model})")
            if current_config.get("ollama_base_url"):
                console.print(f"  [bold]Ollama URL:[/bold] {current_config['ollama_base_url']}")
            console.print()
            continue
        if cmd == "save":
            save_config(current_config)
            console.print("[green]  Config saved to .floe-agent.json\n[/green]")
            continue

        # Send to AI with tool-call loop
        messages.append({"role": "user", "content": trimmed})

        try:
            _run_tool_loop(ai_client, messages, tools, action_map, system_prompt)
        except Exception as e:
            console.print(f"\n[red]  Error: {e}[/red]\n")
            messages.pop()


def _run_tool_loop(
    ai_client: AIClient,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    action_map: dict[str, Any],
    system_prompt: str,
    max_steps: int = 10,
) -> None:
    """Execute the multi-step tool-calling loop."""
    for _step in range(max_steps):
        response = ai_client.chat(messages, tools=tools, system=system_prompt)

        if response.get("tool_calls"):
            # Process tool calls
            if ai_client.provider == "claude":
                # For Anthropic: build assistant message with tool_use blocks
                assistant_content: list[dict[str, Any]] = []
                if response.get("content"):
                    console.print(f"\n[green]Assistant: [/green]{response['content']}")
                    assistant_content.append({"type": "text", "text": response["content"]})
                for tc in response["tool_calls"]:
                    console.print(f"[dim]  [Calling {tc['name']}...][/dim]")
                    assistant_content.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["arguments"],
                        }
                    )
                messages.append({"role": "assistant", "content": assistant_content})

                # Execute and add results
                tool_results = []
                for tc in response["tool_calls"]:
                    result = _execute_tool(tc["name"], tc["arguments"], action_map)
                    preview = result[:150]
                    console.print(
                        f"[dim]  [{tc['name']} done] {preview}{'...' if len(result) > 150 else ''}[/dim]"
                    )
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": result,
                        }
                    )
                messages.append({"role": "user", "content": tool_results})
            else:
                # For OpenAI-compatible: standard tool call format
                assistant_msg: dict[str, Any] = {
                    "role": "assistant",
                    "content": response.get("content"),
                }
                if response.get("content"):
                    console.print(f"\n[green]Assistant: [/green]{response['content']}")
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"]),
                        },
                    }
                    for tc in response["tool_calls"]
                ]
                messages.append(assistant_msg)

                for tc in response["tool_calls"]:
                    console.print(f"[dim]  [Calling {tc['name']}...][/dim]")
                    result = _execute_tool(tc["name"], tc["arguments"], action_map)
                    preview = result[:150]
                    console.print(
                        f"[dim]  [{tc['name']} done] {preview}{'...' if len(result) > 150 else ''}[/dim]"
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result,
                        }
                    )
        else:
            # No tool calls — final text response
            text = response.get("content") or "(action completed)"
            console.print(f"\n[green]Assistant: [/green]{text}\n")
            messages.append({"role": "assistant", "content": text})
            return

    # Max steps reached
    console.print("[yellow]  (max tool steps reached)[/yellow]\n")


def _execute_tool(name: str, arguments: dict[str, Any], action_map: dict[str, Any]) -> str:
    """Execute a Floe action by name."""
    if name not in action_map:
        return f"Unknown tool: {name}"
    action_fn, wallet_provider = action_map[name]
    try:
        return action_fn(wallet_provider, arguments)
    except Exception as e:
        return f"Error executing {name}: {e}"


def _build_tools(
    provider: Any, wallet_provider: Any
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build OpenAI-format tool definitions and action map from the provider."""
    tools: list[dict[str, Any]] = []
    action_map: dict[str, Any] = {}

    for action in provider.get_actions():
        schema = action.schema
        # Convert Pydantic schema to JSON Schema
        json_schema = schema.model_json_schema() if hasattr(schema, "model_json_schema") else {}

        tool_def = {
            "type": "function",
            "function": {
                "name": action.name,
                "description": action.description,
                "parameters": json_schema,
            },
        }
        tools.append(tool_def)
        action_map[action.name] = (action.invoke, wallet_provider)

    return tools, action_map


def _build_system_prompt(address: str, tool_names: list[str]) -> str:
    return f"""You are a DeFi assistant for the Floe lending protocol on Base Mainnet.

Connected wallet: {address}
Network: Base Mainnet (chain ID 8453)

You help users with:
- Checking lending markets, loan details, health factors, and oracle prices
- Posting lend and borrow intents
- Matching intents, repaying loans, managing collateral
- Liquidating unhealthy positions

Available tools: {", ".join(tool_names)}

IMPORTANT: Always confirm with the user before executing write operations (posting intents, repaying, liquidating, etc.). Explain what the transaction will do and its parameters before proceeding.

When displaying transaction hashes, include the BaseScan link: https://basescan.org/tx/<hash>"""


def _discover_market_ids(wallet_provider: Any) -> list[str]:
    """Discover known market IDs from on-chain getMarketId calls."""
    ids: list[str] = []
    for pair in MARKET_PAIRS:
        try:
            market_id = wallet_provider.read_contract(
                contract_address=BASE_MAINNET_MATCHER,
                abi=LENDING_MATCHER_ABI,
                function_name="getMarketId",
                args=[pair["loan"], pair["collateral"]],
            )
            ids.append(str(market_id))
        except Exception as e:
            console.print(f"[dim]  Warning: Failed to resolve {pair['label']}: {str(e)[:80]}[/dim]")
    return ids
