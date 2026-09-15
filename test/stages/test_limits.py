"""Phase 5B (task 5.5): pure-unit coverage of the generic limit helper.

The dual counters (per-auth capped by the board, per-pipeline by config) live in
``jobfucker.limits``; the accounting (read/increment daily_limits) is covered in
the apply and integration suites.
"""

from __future__ import annotations

import pytest

from jobfucker.limits import Limits, today_iso


@pytest.mark.unit
def test_can_apply_allows_below_both_caps() -> None:
    """Applying is allowed while both counters are strictly below their caps."""
    limits = Limits(per_auth_cap=200, per_pipeline_limit=50)
    assert limits.can_apply(used_per_auth=10, used_per_pipeline=10) is True


@pytest.mark.unit
def test_can_apply_blocks_when_per_auth_reached() -> None:
    """Reaching the per-auth cap blocks further applications."""
    limits = Limits(per_auth_cap=200, per_pipeline_limit=50)
    assert limits.can_apply(used_per_auth=200, used_per_pipeline=0) is False
    assert limits.can_apply(used_per_auth=201, used_per_pipeline=0) is False


@pytest.mark.unit
def test_can_apply_blocks_when_per_pipeline_reached() -> None:
    """Reaching the per-pipeline limit blocks further applications."""
    limits = Limits(per_auth_cap=200, per_pipeline_limit=50)
    assert limits.can_apply(used_per_auth=0, used_per_pipeline=50) is False
    assert limits.can_apply(used_per_auth=0, used_per_pipeline=49) is True


@pytest.mark.unit
def test_can_apply_zero_daily_limit_blocks_immediately() -> None:
    """A pipeline configured with ``daily_apply_limit=0`` can never apply."""
    limits = Limits(per_auth_cap=200, per_pipeline_limit=0)
    assert limits.can_apply(used_per_auth=0, used_per_pipeline=0) is False


@pytest.mark.unit
def test_stop_reason_is_none_below_both_caps() -> None:
    """No stop reason while both caps are unbound — applying may continue."""
    limits = Limits(per_auth_cap=200, per_pipeline_limit=50)
    assert limits.stop_reason(10) is None


@pytest.mark.unit
def test_stop_reason_names_the_pipeline_cap() -> None:
    """A bound pipeline cap is named with its current/limit numbers."""
    limits = Limits(per_auth_cap=200, per_pipeline_limit=50)
    assert limits.stop_reason(50) == "daily limit reached — 50 of 50 applied today (pipeline cap)"


@pytest.mark.unit
def test_stop_reason_names_the_board_per_account_cap() -> None:
    """A bound per-auth cap (pipeline cap not yet bound) names the board cap."""
    limits = Limits(per_auth_cap=200, per_pipeline_limit=300)
    assert limits.stop_reason(200) == "daily limit reached — 200 of 200 applied today (board per-account cap)"


@pytest.mark.unit
def test_stop_reason_pipeline_cap_wins_when_both_caps_bound() -> None:
    """When both caps are bound at once, the more specific pipeline cap names itself."""
    limits = Limits(per_auth_cap=5, per_pipeline_limit=5)
    assert limits.stop_reason(5) == "daily limit reached — 5 of 5 applied today (pipeline cap)"


@pytest.mark.unit
def test_today_iso_is_yyyymmdd() -> None:
    """The counter key is ``YYYY-MM-DD`` (e.g. a 10-char ISO date)."""
    value = today_iso()
    assert len(value) == 10
    assert value[4] == "-" and value[7] == "-"
    int(value[:4])  # year parses as an integer
