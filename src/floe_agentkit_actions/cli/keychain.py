"""OS keychain wrapper for Floe agent API keys.

Uses the `keyring` package (macOS Keychain, Windows Credential Manager,
Linux Secret Service via DBus). Falls back to an env var
``FLOE_AGENT_KEY_<UPPER_NAME>`` when the keyring backend is unavailable
— e.g. headless CI without a session keyring. A one-time warning is
printed on first fallback so the user knows their secret isn't
persisted in the OS store.

Account format: ``<agent_name>@<facilitator_url>`` — the URL is included
so the same agent name against staging vs prod doesn't collide.
"""

from __future__ import annotations

import os
import re

SERVICE = "floe-agent"
_warned_fallback = False


def _env_var_name(agent_name: str) -> str:
    sanitized = re.sub(r"[^A-Z0-9]", "_", agent_name.upper())
    return f"FLOE_AGENT_KEY_{sanitized}"


def _account(agent_name: str, facilitator_url: str) -> str:
    return f"{agent_name}@{facilitator_url}"


def _try_load_keyring() -> object | None:
    """Return the `keyring` module, or None if the backend is unusable."""
    global _warned_fallback
    try:
        import keyring  # noqa: PLC0415 — lazy import; module load shouldn't fail without OS keychain.
        from keyring.errors import NoKeyringError  # noqa: PLC0415

        # Probe the backend — set_password() raises NoKeyringError on
        # backends like keyring.backends.fail.Keyring.
        backend = keyring.get_keyring()
        if backend.__class__.__name__ == "Keyring" and backend.__class__.__module__.endswith(".fail"):
            raise NoKeyringError("No usable keyring backend")
        return keyring
    except Exception as err:  # noqa: BLE001 — broad on purpose; any failure → env-var fallback.
        if not _warned_fallback:
            print(
                "  Warning: OS keyring unavailable; falling back to env vars.\n"
                f"    Set {_env_var_name('<name>')} per agent.\n"
                f"    ({err})"
            )
            _warned_fallback = True
        return None


def set_agent_key(agent_name: str, facilitator_url: str, api_key: str) -> str:
    """Persist a key. Returns 'keychain' or 'env-fallback' indicating how it was stored."""
    kr = _try_load_keyring()
    if kr is None:
        print(
            f"  Keyring unavailable. To use this agent later, export:\n"
            f"    {_env_var_name(agent_name)}={api_key}\n"
            "  This key will not be shown again."
        )
        return "env-fallback"
    kr.set_password(SERVICE, _account(agent_name, facilitator_url), api_key)  # type: ignore[attr-defined]
    return "keychain"


def get_agent_key(agent_name: str, facilitator_url: str) -> str | None:
    """Look up a key. Env var takes precedence over the OS keychain."""
    env_value = os.environ.get(_env_var_name(agent_name))
    if env_value and env_value.strip():
        return env_value.strip()
    kr = _try_load_keyring()
    if kr is None:
        return None
    try:
        value = kr.get_password(SERVICE, _account(agent_name, facilitator_url))  # type: ignore[attr-defined]
        return str(value) if value is not None else None
    except Exception:  # noqa: BLE001
        return None


def delete_agent_key(agent_name: str, facilitator_url: str) -> bool:
    kr = _try_load_keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(SERVICE, _account(agent_name, facilitator_url))  # type: ignore[attr-defined]
        return True
    except Exception:  # noqa: BLE001
        return False
