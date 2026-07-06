"""Exported action count smoke test.

Validates that both providers export the expected number of actions, and
documents the current gap against the TypeScript reference
(`agentkit-actions`).

Current state (July 2026 — FLO-602 inference actions, Python first):

- TypeScript agentkit-actions: FloeActionProvider=30 + X402ActionProvider=22 = 52
- Python agentkit-actions-py:  FloeActionProvider=30 + X402ActionProvider=24 = 54

X402ActionProvider has 6 credit-delegation actions (grant/revoke/check +
x402_fetch + x402_get_balance + x402_get_transactions) + 9 agent-awareness
actions (get_credit_remaining, get_loan_state, {get,set,clear}_spend_limit,
{list,register,delete}_credit_threshold, estimate_x402_cost) + 1 managed
credit-line action (open_credit_line) added in v0.4 + 1 settlement helper
(x402_await_settlement) added in FLO-567 + 5 merchant-allowlist actions
({set,get}_allowlist_mode, {add,remove}_allowlist_entry, list_allowlist) + 2
FLO-602 inference actions (list_inference_models, estimate_inference_cost).

Python is currently 2 actions AHEAD of TypeScript: the FLO-602 inference
actions (list_inference_models, estimate_inference_cost) have not landed in
the TypeScript port yet. PARITY_GAP = -2 until they do.

If either provider's action count changes, update the constants below.
If parity breaks further, fix the port — do not just bump PARITY_GAP to hide
the drift. Update floe-labs-docs whenever the user-facing counts change.
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
# Python is 2 actions ahead: the 2 FLO-602 inference actions
# (list_inference_models, estimate_inference_cost) have NOT landed in the
# TypeScript port yet — verified: agentkit-actions x402ActionProvider exports
# 22 @CreateAction decorators + 30 in floeActionProvider = 52 vs 54 in Python.
# Once they land in TypeScript, bump TS_REFERENCE_TOTAL to 54 (gap back to 0).
TS_REFERENCE_TOTAL = 52
PARITY_GAP = TS_REFERENCE_TOTAL - TOTAL_ACTION_COUNT  # -2 (Python ahead of TS)


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
    """Assert that the documented Python/TypeScript parity gap stays accurate.

    As of the FLO-602 PR the gap is -2 (Python ahead). If a new action lands
    in TypeScript ahead of Python (or vice versa), update the constants at
    the top of this file AND floe-labs-docs in the same PR.
    """
    total = _count_actions(FloeActionProvider) + _count_actions(X402ActionProvider)
    gap = TS_REFERENCE_TOTAL - total
    assert gap == PARITY_GAP, (
        f"Parity gap between TS ({TS_REFERENCE_TOTAL}) and Python ({total}) "
        f"is {gap}, expected {PARITY_GAP}. If the gap changed, update the "
        f"constants AND floe-labs-docs if the user-facing count is affected."
    )
