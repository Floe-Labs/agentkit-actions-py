"""Entry point for `python -m floe_agentkit_actions.cli` and the `floe-agent` command."""

from __future__ import annotations


def main() -> None:
    from .app import run

    run()


if __name__ == "__main__":
    main()
