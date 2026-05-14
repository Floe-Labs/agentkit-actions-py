"""Wallet-signature auth headers for the Floe Developer API.

Mirrors the server-side message format at
``apps/api/src/middleware/auth.ts:47``::

    "Floe Credit API\\nTimestamp: <unix-seconds>"

The 5-minute drift tolerance is enforced server-side.
"""

from __future__ import annotations

import time
from typing import Any

MESSAGE_PREFIX = "Floe Credit API\nTimestamp: "


def build_auth_headers(wallet_provider: Any) -> dict[str, str]:
    """Build EIP-191-signed auth headers from any AgentKit EvmWalletProvider."""
    address = wallet_provider.get_address()
    timestamp = str(int(time.time()))
    message = f"{MESSAGE_PREFIX}{timestamp}"
    signature = wallet_provider.sign_message(message)
    return {
        "X-Wallet-Address": address,
        "X-Signature": signature,
        "X-Timestamp": timestamp,
        "Content-Type": "application/json",
    }
