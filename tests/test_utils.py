"""Tests for utility formatting functions."""

from floe_agentkit_actions.utils import (
    compute_health_percent,
    format_address,
    format_bps,
    format_duration,
    format_price,
    format_timestamp,
    format_token_amount,
)


def test_format_bps():
    assert format_bps(500) == "5.00%"
    assert format_bps(10000) == "100.00%"
    assert format_bps(0) == "0.00%"
    assert format_bps(50) == "0.50%"


def test_format_token_amount():
    # 1 USDC (6 decimals)
    result = format_token_amount(1_000_000, 6, "USDC")
    assert "1.00" in result
    assert "USDC" in result

    # 1 WETH (18 decimals)
    result = format_token_amount(10**18, 18, "WETH")
    assert "1.00" in result
    assert "WETH" in result

    # Without symbol
    result = format_token_amount(1_000_000, 6)
    assert "1.00" in result
    assert "USDC" not in result


def test_format_duration():
    assert format_duration(30) == "30 seconds"
    assert format_duration(120) == "2 minutes"
    assert format_duration(7200) == "2 hours"
    assert format_duration(86400) == "1 day"
    assert format_duration(172800) == "2 days"


def test_format_timestamp():
    assert format_timestamp(0) == "N/A"
    result = format_timestamp(1700000000)
    assert "2023" in result  # Nov 14 2023


def test_format_address():
    addr = "0x1234567890abcdef1234567890abcdef12345678"
    assert format_address(addr) == "0x1234...5678"
    assert format_address("0x12") == "0x12"


def test_format_price():
    scale = 10**36
    # 1.0 price
    assert "1.00" in format_price(scale)
    # Small price
    assert "0." in format_price(1)


def test_compute_health_percent():
    assert compute_health_percent(0, 0) == "N/A"
    result = compute_health_percent(6500, 8500)
    assert "23.5%" == result
    result = compute_health_percent(8500, 8500)
    assert "0.0%" == result
