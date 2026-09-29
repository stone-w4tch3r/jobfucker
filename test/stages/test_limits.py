"""Pure-unit coverage of the generic quota helper.

Two independent counters (per-auth capped by the board, per-pipeline by config)
count over one board-declared window; the storage accounting is covered in the
apply, storage and integration suites.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobfucker.limits import Limits, period_key


@pytest.mark.unit
def test_can_apply_allows_below_both_caps() -> None:
    """Applying is allowed while both counters are strictly below their caps."""
    limits = Limits(period="day", per_auth_cap=200, per_pipeline_limit=50)
    assert limits.can_apply(used_per_auth=10, used_per_pipeline=10) is True


@pytest.mark.unit
def test_can_apply_blocks_when_per_auth_reached() -> None:
    """Reaching the per-auth (account, shared) cap blocks further applications."""
    limits = Limits(period="day", per_auth_cap=200, per_pipeline_limit=50)
    assert limits.can_apply(used_per_auth=200, used_per_pipeline=0) is False
    assert limits.can_apply(used_per_auth=201, used_per_pipeline=0) is False


@pytest.mark.unit
def test_can_apply_blocks_when_per_pipeline_reached() -> None:
    """Reaching this pipeline's own limit blocks it even while the account has room."""
    limits = Limits(period="day", per_auth_cap=200, per_pipeline_limit=50)
    assert limits.can_apply(used_per_auth=0, used_per_pipeline=50) is False
    assert limits.can_apply(used_per_auth=0, used_per_pipeline=49) is True


@pytest.mark.unit
def test_can_apply_zero_limit_blocks_immediately() -> None:
    """A pipeline configured with ``apply_limit=0`` can never apply."""
    limits = Limits(period="day", per_auth_cap=200, per_pipeline_limit=0)
    assert limits.can_apply(used_per_auth=0, used_per_pipeline=0) is False


@pytest.mark.unit
def test_stop_reason_is_none_below_both_caps() -> None:
    """No stop reason while both caps are unbound — applying may continue."""
    limits = Limits(period="day", per_auth_cap=200, per_pipeline_limit=50)
    assert limits.stop_reason(10, 10) is None


@pytest.mark.unit
def test_stop_reason_names_the_pipeline_cap() -> None:
    """A bound pipeline cap is named with its current/limit numbers."""
    limits = Limits(period="day", per_auth_cap=200, per_pipeline_limit=50)
    assert limits.stop_reason(0, 50) == "daily limit reached — 50 of 50 applied today (pipeline cap)"


@pytest.mark.unit
def test_stop_reason_names_the_board_per_account_cap() -> None:
    """A bound per-auth cap (pipeline cap not yet bound) names the board cap."""
    limits = Limits(period="day", per_auth_cap=200, per_pipeline_limit=300)
    assert limits.stop_reason(200, 40) == ("daily limit reached — 200 of 200 applied today (board per-account cap)")


@pytest.mark.unit
def test_stop_reason_pipeline_cap_wins_when_both_caps_bound() -> None:
    """When both caps are bound at once, the more specific pipeline cap names itself."""
    limits = Limits(period="day", per_auth_cap=5, per_pipeline_limit=5)
    assert limits.stop_reason(5, 5) == "daily limit reached — 5 of 5 applied today (pipeline cap)"


@pytest.mark.unit
def test_month_period_wording() -> None:
    """A monthly board words the stop line as ``this month``."""
    limits = Limits(period="month", per_auth_cap=150, per_pipeline_limit=150)
    assert limits.stop_reason(150, 150) == ("monthly limit reached — 150 of 150 applied this month (pipeline cap)")


@pytest.mark.unit
def test_period_key_day_is_iso_date() -> None:
    """A day window keys as ``YYYY-MM-DD``."""
    moment = datetime(2026, 8, 5, 23, 30, tzinfo=UTC)
    assert period_key("day", now=moment) == "2026-08-05"


@pytest.mark.unit
def test_period_key_month_is_year_month() -> None:
    """A month window keys as ``YYYY-MM`` (a new key each calendar month resets the counter)."""
    assert period_key("month", now=datetime(2026, 8, 5, tzinfo=UTC)) == "2026-08"
    assert period_key("month", now=datetime(2026, 9, 1, tzinfo=UTC)) == "2026-09"
