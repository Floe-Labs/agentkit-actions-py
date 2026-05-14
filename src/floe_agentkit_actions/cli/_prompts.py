"""Shared CLI prompt helpers.

questionary's ``.ask()`` returns ``None`` when the user cancels the
prompt (Ctrl-C / EOF). Letting that ``None`` flow into downstream code
(e.g. into a wallet config or ``int()``) produces opaque ``TypeError`` /
``ValueError`` instead of a clean exit. The helpers here normalise
cancellation into a graceful ``sys.exit(1)`` with a readable message.
"""

from __future__ import annotations

import sys

from rich.console import Console

_console = Console()


def require_prompt(value: str | None, label: str) -> str:
    """Return ``value`` or exit cleanly if the user cancelled the prompt."""
    if value is None or value.strip() == "":
        _console.print(f"[red]{label} is required.[/red]")
        sys.exit(1)
    return value


def prompt_int(message: str, default: str) -> int:
    """Prompt for an integer; exit cleanly if cancelled or not parseable."""
    import questionary  # noqa: PLC0415 — lazy import keeps module loadable without TUI.

    raw = questionary.text(message, default=default).ask()
    if raw is None:
        _console.print("[red]Input required.[/red]")
        sys.exit(1)
    try:
        return int(raw)
    except ValueError:
        _console.print(f"[red]Expected an integer, got {raw!r}.[/red]")
        sys.exit(1)
