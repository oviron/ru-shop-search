"""Regressions for defects an independent multi-model review found in R3/R4 (2026-09-18)."""

from __future__ import annotations

from mcp_core.transport.cdp_budget import NavigationBudget


async def test_our_own_policy_error_does_not_reset_the_hosts_refusal_record():
    budget = NavigationBudget(max_concurrent=2, breaker_threshold=2, breaker_cooldown_s=60.0)
    async with budget.slot("policy.example") as slot:
        slot.refused(403)
    permit = await budget.acquire("policy.example")
    permit.neutral()
    permit.release()
    assert budget.snapshot()["hosts"]["policy.example"]["refusals"] == 1
    async with budget.slot("policy.example") as slot:
        slot.refused(503)
    assert budget.snapshot()["hosts"]["policy.example"]["open"] is True
