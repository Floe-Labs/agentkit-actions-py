"""Exported action count smoke test.

Validates that both providers export the expected number of actions, and
asserts that the Python port remains at full parity with the TypeScript
reference (`agentkit-actions`).

Current state (June 2026 — v0.5 CrewAI integration + D1 merchant allowlist):

- TypeScript agentkit-actions: FloeActionProvider=30 + X402ActionProvider=24 = 54
- Python agentkit-actions-py:  FloeActionProvider=30 + X402ActionProvider=24 = 54

X402ActionProvider has 6 credit-delegation actions (grant/revoke/check +
x402_fetch + x402_get_balance + x402_get_transactions) + 9 agent-awareness
actions (get_credit_remaining, get_loan_state, {get,set,clear}_spend_limit,
{list,register,delete}_credit_threshold, estimate_x402_cost) + 1 managed
credit-line action (open_credit_line) added in v0.4 + 1 settlement helper
(x402_await_settlement) added in FLO-567 + 5 merchant-allowlist actions
({set,get}_allowlist_mode, {add,remove}_allowlist_entry, list_allowlist) + 2
FLO-602 inference actions (list_inference_models, estimate_inference_cost).

Full parity: both ports are at 54 (the allowlist + inference actions have landed
in TypeScript too). PARITY_GAP = 0.

If either provider's action count changes, update the constants below.
If parity breaks, fix the port — do not just bump PARITY_GAP to hide the drift.
The docs at floe-labs-docs claim "54 actions in both TypeScript and Python" — keep that true.
"""

from __future__ import annotations

from floe_agentkit_actions.action_provider import FloeActionProvider
from floe_agentkit_actions.x402_action_provider import X402ActionProvider

# If these numbers change, it means either:
#   (a) Someone added a new action — bump the expected count and update
#       floe-labs-docs if the combined total changed
#   (b) Someone removed an action — investigate whether that was intentional
FLOE_PROVIDER_ACTION_COUNT = 30  # full TS parity: 23 base + 7 credit-facility
X402_PROVIDER_ACTION_COUNT = 24  # 6 x402 delegation + 9 agent-awareness + 1 open_credit_line (v0.4) + 1 x402_await_settlement (FLO-567) + 5 D1 merchant-allowlist + 2 FLO-602 inference (list_inference_models, estimate_inference_cost)
TOTAL_ACTION_COUNT = FLOE_PROVIDER_ACTION_COUNT + X402_PROVIDER_ACTION_COUNT  # 54

# The TypeScript reference port. Gap = TS - Python.
#
# FULL PARITY: the 5 merchant-allowlist actions (D9) AND the 2 FLO-602 inference
# actions (list_inference_models, estimate_inference_cost) have now landed in the
# TypeScript port as well — verified: agentkit-actions x402ActionProvider exports
# 24 @CreateAction decorators + 30 in floeActionProvider = 54, identical to Python.
# So the ports are in lockstep at 54 and the parity gap is 0.
TS_REFERENCE_TOTAL = 54
PARITY_GAP = TS_REFERENCE_TOTAL - TOTAL_ACTION_COUNT  # 0 (TS and Python both at 54)


def _count_actions(provider_cls) -> int:
    """Instantiate the provider and count its exported actions.

    coinbase-agentkit's @create_action decorator attaches metadata to
    methods. The provider base class exposes them via get_actions().
    """
    from unittest.mock import MagicMock
    provider = provider_cls()
    wallet = MagicMock()
    wallet.get_address = MagicMock(return_value="0x" + "11" * 20)
    wallet.get_network = MagicMock(return_value=MagicMock(chain_id="8453"))
    actions = provider.get_actions(wallet)
    return len(actions)


def test_floe_provider_exports_expected_action_count() -> None:
    count = _count_actions(FloeActionProvider)
    assert count == FLOE_PROVIDER_ACTION_COUNT, (
        f"FloeActionProvider exports {count} actions, expected "
        f"{FLOE_PROVIDER_ACTION_COUNT}. If this changed intentionally, update "
        f"the constant and bump TOTAL_ACTION_COUNT."
    )


def test_x402_provider_exports_expected_action_count() -> None:
    count = _count_actions(X402ActionProvider)
    assert count == X402_PROVIDER_ACTION_COUNT, (
        f"X402ActionProvider exports {count} actions, expected "
        f"{X402_PROVIDER_ACTION_COUNT}."
    )


def test_total_action_count_matches_current_python_state() -> None:
    total = _count_actions(FloeActionProvider) + _count_actions(X402ActionProvider)
    assert total == TOTAL_ACTION_COUNT


def test_python_port_parity_gap_is_documented() -> None:
    """Assert that the documented Python/TypeScript parity gap stays closed.

    As of commit 854fd92 the gap is 0. If a new action lands in TypeScript
    ahead of Python (or vice versa), update the constants at the top of
    this file AND floe-labs-docs in the same PR.
    """
    total = _count_actions(FloeActionProvider) + _count_actions(X402ActionProvider)
    gap = TS_REFERENCE_TOTAL - total
    assert gap == PARITY_GAP, (
        f"Parity gap between TS ({TS_REFERENCE_TOTAL}) and Python ({total}) "
        f"is {gap}, expected {PARITY_GAP}. If the gap changed, update the "
        f"constants AND floe-labs-docs if the user-facing count is affected."
    )
