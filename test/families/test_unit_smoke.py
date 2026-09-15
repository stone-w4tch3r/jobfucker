"""Phase 1 (task 1.3): pure unit smoke for the ``unit`` family.

Proves the ``unit`` marker collects and a pure (side-effect-free) function can be
asserted directly. The helper below mirrors the daily-apply-limit arithmetic
that the apply stage (Phase 5) will compute; it lives here solely to smoke-test
the harness, and the real implementation lands in ``src/jobfucker/limits.py``.
"""

from __future__ import annotations

import pytest


def remaining_after_daily_apply(per_auth_cap: int, already_applied: int) -> int:
    """Return how many applications remain against a daily cap (never negative)."""
    return max(0, per_auth_cap - already_applied)


@pytest.mark.unit
def test_unit_smoke_remaining_budget() -> None:
    """Pure arithmetic: remaining is the cap minus what was applied, floored at 0."""
    cap: int = 200
    applied_below: int = 3
    applied_over: int = 250

    assert remaining_after_daily_apply(cap, applied_below) == cap - applied_below
    assert remaining_after_daily_apply(cap, applied_over) == 0
