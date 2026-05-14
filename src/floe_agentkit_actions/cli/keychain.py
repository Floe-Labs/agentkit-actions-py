"""OS keychain wrapper for Floe agent API keys.

Uses the `keyring` package (macOS Keychain, Windows Credential Manager,
Linux Secret Service via DBus). Falls back to env vars when the keyring
backend is unavailable — e.g. headless CI without a session keyring. A
one-time warning is printed on first fallback so the user knows their
secret isn't persisted in the OS store.

Account format: ``<agent_name>@<facilitator_url>`` — the URL is included
so the same agent name against staging vs prod doesn't collide.

Env-var fallback name:
  - Primary:  ``FLOE_AGENT_KEY_<NAME>__<HOST>`` (scoped per facilitator)
  - Fallback: ``FLOE_AGENT_KEY_<NAME>``        (legacy, pre-v0.4.1)

``get_agent_key`` reads the scoped name first and falls back to the
legacy name so users with ``FLOE_AGENT_KEY_ALPHA`` already exported keep
working. The same agent name across two facilitators (staging vs prod)
can now carry distinct credentials without collision via the host suffix.
"""

from __future__ import annotations

import os
import re
from urllib.parse import urlparse

SERVICE = "floe-agent"
_warned_fallback = False


def _normalize_for_env(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "_", s.upper())


def _legacy_env_var_name(agent_name: str) -> str:
    return f"FLOE_AGENT_KEY_{_normalize_for_env(agent_name)}"


def _scoped_env_var_name(agent_name: str, facilitator_url: str) -> str:
    host: str | None = None
    try:
        parsed = urlparse(facilitator_url)
        host = parsed.netloc or None
    except Exception:  # noqa: BLE001 — bad URL → fall back to legacy.
        host = None
    if not host:
        return _legacy_env_var_name(agent_name)
    return f"FLOE_AGENT_KEY_{_normalize_for_env(agent_name)}__{_normalize_for_env(host)}"


# Back-compat alias for callers that import the unscoped helper.
def _env_var_name(agent_name: str) -> str:
    return _legacy_env_var_name(agent_name)


def env_var_name_for(agent_name: str, facilitator_url: str) -> str:
    """Return the env-var name the CLI uses as a keychain fallback.

    Exported so commands (register, rotate) can surface it in user-facing
    instructions when the keychain write actually fails.
    """
    return _scoped_env_var_name(agent_name, facilitator_url)


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
                f"    Set {_scoped_env_var_name('<name>', 'https://<facilitator>')} per agent.\n"
                f"    ({err})"
            )
            _warned_fallback = True
        return None


def set_agent_key(agent_name: str, facilitator_url: str, api_key: str) -> str:
    """Persist a key. Returns 'keychain' or 'env-fallback' indicating how it was stored.

    On env-fallback we deliberately do NOT echo the raw key here. The
    caller (register / rotate) is responsible for showing it once on
    stdout; including the key in a copy-pasteable ``export …=<key>`` line
    lands it in shell history and CI logs, breaking the "shown once,
    captured manually" contract.
    """
    kr = _try_load_keyring()
    if kr is None:
        env_name = _scoped_env_var_name(agent_name, facilitator_url)
        print(
            "  Keyring unavailable. To load this agent on next run, export:\n"
            f"    export {env_name}=\"<paste the API key shown above>\"\n"
            "  (The key is printed once by the register/rotate command — capture it from there.)"
        )
        return "env-fallback"
    kr.set_password(SERVICE, _account(agent_name, facilitator_url), api_key)  # type: ignore[attr-defined]
    return "keychain"


def get_agent_key(agent_name: str, facilitator_url: str) -> str | None:
    """Look up a key. Env var (scoped, then legacy) takes precedence over the OS keychain."""
    scoped = os.environ.get(_scoped_env_var_name(agent_name, facilitator_url))
    if scoped and scoped.strip():
        return scoped.strip()
    legacy = os.environ.get(_legacy_env_var_name(agent_name))
    if legacy and legacy.strip():
        return legacy.strip()
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
