"""FLO-552: x402_fetch should surface the dollar amount paid in its note.

Locks down src/floe_agentkit_actions/x402_action_provider.py x402_fetch:
the markdown note must include the paid USDC amount, preferring the decimal
X-Floe-Payment-Amount header and falling back to formatting the raw-units
X-Floe-Cost-USDC header.
"""

from __future__ import annotations

from typing import Any

from floe_agentkit_actions.x402_action_provider import X402ActionProvider, X402Config


class NoopWallet:
    def get_address(self) -> str:
        return "0x1111111111111111111111111111111111111111"

    def get_name(self) -> str:
        return "noop-wallet"

    def get_network(self) -> Any:
        from unittest.mock import MagicMock

        net = MagicMock()
        net.chain_id = "8453"
        net.network_id = "base-mainnet"
        net.protocol_family = "evm"
        return net

    def sign_message(self, _message: str) -> str:
        return "0xsig"


def _provider_with_headers(headers: dict[str, str]) -> X402ActionProvider:
    provider = X402ActionProvider(X402Config(
        matcher_address="0x17946cD3e180f82e632805e5549EC913330Bb175",
        facilitator_url="https://credit-api.floelabs.xyz",
    ))

    def _spy(path: str, method: str = "GET", body: Any = None) -> dict[str, Any]:
        return {"status": 200, "body": {"ok": True}, "headers": headers}

    provider._facilitator_fetch = _spy  # type: ignore[method-assign]
    return provider


def test_note_uses_payment_amount_header() -> None:
    provider = _provider_with_headers({
        "x-floe-payment-amount": "0.001000",
        "payment-response": "0xabc",
    })
    note = provider.x402_fetch(NoopWallet(), {"url": "https://example.com"})
    assert "$0.001000 USDC" in note
    assert "tx: 0xabc" in note


def test_note_falls_back_to_cost_usdc_raw() -> None:
    provider = _provider_with_headers({"x-floe-cost-usdc": "1000"})
    note = provider.x402_fetch(NoopWallet(), {"url": "https://example.com"})
    assert "$0.001000 USDC" in note


def test_note_without_payment_headers_has_no_paid_line() -> None:
    provider = _provider_with_headers({})
    note = provider.x402_fetch(NoopWallet(), {"url": "https://example.com"})
    assert "Paid via x402" not in note
