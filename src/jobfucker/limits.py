"""Generic daily-limit enforcement shared by the apply stage (Phase 5B, task 5.5).

There is **one shared per-auth counter** per ``(service, login, date)`` in
``daily_limits`` — shared across every pipeline that uses the account, so a cap
is never double-counted. Before each application the apply stage checks that
single count against **both** caps:

- **per-auth** — the board's declared per-auth daily cap
  (``service_info.per_auth_daily_cap``, read from the client, never a constant
  in core);
- **per-pipeline** — the pipeline's configured ``daily_apply_limit``.

This module holds the generic "can we apply yet?" logic and the date helper. It
contains **no cap constants** — the caps are injected from the client's
``service_info`` and the pipeline config. The concrete accounting (reading the
``daily_limits`` counter and incrementing it on success) lives in
:mod:`jobfucker.stages.apply`, which composes this helper.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

__all__ = ["Limits", "today_iso"]


@dataclass(frozen=True, slots=True)
class Limits:
    """The two caps the apply stage enforces against the **one shared** auth counter.

    Args:
        per_auth_cap: the board's per-auth daily cap (``service_info``), e.g. 200.
        per_pipeline_limit: the pipeline's own ``daily_apply_limit``.

    Both caps are checked against the same shared ``(service, login, date)``
    count, so the effective stop is the tighter of the two.
    """

    per_auth_cap: int
    per_pipeline_limit: int

    def can_apply(self, used_per_auth: int, used_per_pipeline: int) -> bool:
        """Whether another application is allowed given the current usage.

        Both caps are applied to the **same shared per-auth count** — the apply
        stage passes that single value for both parameters (``used_per_auth``
        and ``used_per_pipeline`` are the one count seen from each cap's
        perspective). The count must be strictly below both caps; reaching
        either stops further applications (an ``Ok`` report with ``limit_reached``,
        never an error).
        """
        return used_per_auth < self.per_auth_cap and used_per_pipeline < self.per_pipeline_limit

    def stop_reason(self, used: int) -> str | None:
        """Why another application is not allowed; ``None`` while one is.

        The tighter of the two caps names itself in the wording, so the printed
        stop line always says which cap bound and how much of it is used. The
        pipeline cap wins when both are bound (it is the more specific one).
        """
        if used >= self.per_pipeline_limit:
            return f"daily limit reached — {used} of {self.per_pipeline_limit} applied today (pipeline cap)"
        if used >= self.per_auth_cap:
            return f"daily limit reached — {used} of {self.per_auth_cap} applied today (board per-account cap)"
        return None


def today_iso() -> str:
    """Today's date as ``YYYY-MM-DD`` — the key the daily_limits counters use.

    Injectable where determinism matters (tests pass an explicit ``today``).
    """
    return datetime.now(UTC).date().isoformat()
