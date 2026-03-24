"""Formatting and utility functions for Floe actions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .constants import ERC20_ABI, KNOWN_TOKENS, ORACLE_PRICE_SCALE

if TYPE_CHECKING:
    pass


def format_bps(bps: int) -> str:
    """Convert basis points to percentage string."""
    percent = bps / 100
    return f"{percent:.2f}%"


def format_token_amount(amount: int, decimals: int, symbol: str | None = None) -> str:
    """Format a raw token amount to human-readable string."""
    divisor = 10**decimals
    value = amount / divisor
    max_frac = min(decimals, 8)
    formatted = f"{value:,.{max_frac}f}"
    # Strip trailing zeros but keep at least 2 decimal places
    if "." in formatted:
        parts = formatted.split(".")
        frac = parts[1].rstrip("0")
        if len(frac) < 2:
            frac = frac.ljust(2, "0")
        formatted = f"{parts[0]}.{frac}"
    return f"{formatted} {symbol}" if symbol else formatted


def format_duration(seconds: int) -> str:
    """Convert seconds to human-readable duration."""
    s = int(seconds)
    if s < 60:
        return f"{s} seconds"
    if s < 3600:
        return f"{s // 60} minutes"
    if s < 86400:
        return f"{s // 3600} hours"
    days = s // 86400
    return "1 day" if days == 1 else f"{days} days"


def format_timestamp(ts: int) -> str:
    """Convert unix timestamp to UTC date string."""
    if ts == 0:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def format_address(addr: str) -> str:
    """Shorten an address for display."""
    if len(addr) < 10:
        return addr
    return f"{addr[:6]}...{addr[-4:]}"


def format_price(price: int, scale: int = ORACLE_PRICE_SCALE) -> str:
    """Format a price from the oracle scale."""
    value = price / scale
    if value >= 1:
        return f"{value:,.6f}"
    return f"{value:.8f}"


def resolve_token_meta(
    address: str, wallet_provider: Any
) -> dict[str, Any]:
    """Resolve token symbol and decimals, checking known tokens first."""
    lower = address.lower()
    for known_addr, meta in KNOWN_TOKENS.items():
        if known_addr.lower() == lower:
            return meta

    try:
        symbol = wallet_provider.read_contract(
            contract_address=address,
            abi=ERC20_ABI,
            function_name="symbol",
            args=[],
        )
        decimals = wallet_provider.read_contract(
            contract_address=address,
            abi=ERC20_ABI,
            function_name="decimals",
            args=[],
        )
        return {"symbol": str(symbol), "decimals": int(decimals)}
    except Exception:
        return {"symbol": format_address(address), "decimals": 18}


def compute_health_percent(current_ltv_bps: int, liquidation_ltv_bps: int) -> str:
    """Compute health buffer percentage."""
    if liquidation_ltv_bps == 0:
        return "N/A"
    health = (liquidation_ltv_bps - current_ltv_bps) / liquidation_ltv_bps * 100
    return f"{health:.1f}%"
