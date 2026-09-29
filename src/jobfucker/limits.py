"""Generic application-quota enforcement shared by the apply stage.

The board declares the quota **window** it counts over
(``service_info.apply_period``: ``"day"`` for HH, ``"month"`` for Habr) and its
per-auth cap for that window (``service_info.per_auth_apply_cap``). The apply
stage keeps **two independent counters**, both keyed by that same window:

- **per-auth** — one counter per ``(service, login)``, shared across every
  pipeline on the account, so the account's cap is never double-counted;
- **per-pipeline** — one counter per pipeline, capped by the pipeline's
  configured ``apply_limit``.

A window change (a new day / a new month) is a new period key, so a counter
never needs an explicit reset. The effective stop is the tighter of the two
caps. This module holds only the generic "can we apply yet?" logic and the
period-key helper: **no cap constants** (caps are injected from the client's
``service_info`` and the pipeline config) and no storage accounting (that lives
in :mod:`jobfucker.stages.apply`, which composes this helper).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from jobfucker.clients.base import QuotaPeriod

__all__ = ["Limits", "QuotaPeriod", "period_key"]

# Display vocabulary per window: (adjective for the limit line, phrase for "when").
_PERIOD_NOUN: Final[dict[QuotaPeriod, tuple[str, str]]] = {
    "day": ("daily", "today"),
    "month": ("monthly", "this month"),
}


@dataclass(frozen=True, slots=True)
class Limits:
    """The two caps the apply stage enforces over one board-declared window.

    Args:
        period: the board's quota window (``ServiceInfo.apply_period``).
        per_auth_cap: the board's per-auth cap for that window, e.g. 200/day
            (HH) or 150/month (Habr).
        per_pipeline_limit: the pipeline's own ``apply_limit``.

    Both caps count over the same window but against two **separate** counters,
    so a pipeline's own throttle is never mixed with what other pipelines on the
    account already used.
    """

    period: QuotaPeriod
    per_auth_cap: int
    per_pipeline_limit: int

    def can_apply(self, used_per_auth: int, used_per_pipeline: int) -> bool:
        """Whether another application is allowed given the two current usages.

        Each count must be strictly below its own cap; reaching either stops
        further applications (an ``Ok`` report with ``limit_reached``, never an
        error).
        """
        return used_per_auth < self.per_auth_cap and used_per_pipeline < self.per_pipeline_limit

    def stop_reason(self, used_per_auth: int, used_per_pipeline: int) -> str | None:
        """Why another application is not allowed; ``None`` while one is.

        The tighter of the two caps names itself in the wording, so the printed
        stop line always says which cap bound and how much of it is used. The
        pipeline cap wins when both are bound (it is the more specific one).
        """
        adjective, phrase = _PERIOD_NOUN[self.period]
        if used_per_pipeline >= self.per_pipeline_limit:
            return (
                f"{adjective} limit reached — {used_per_pipeline} of "
                f"{self.per_pipeline_limit} applied {phrase} (pipeline cap)"
            )
        if used_per_auth >= self.per_auth_cap:
            return (
                f"{adjective} limit reached — {used_per_auth} of "
                f"{self.per_auth_cap} applied {phrase} (board per-account cap)"
            )
        return None


def period_key(period: QuotaPeriod, *, now: datetime | None = None) -> str:
    """The counter key for the window containing ``now``.

    ``"YYYY-MM-DD"`` for a day, ``"YYYY-MM"`` for a month — the key the two
    counters use. Injectable where determinism matters (tests pass an explicit
    ``now``); the boundary (calendar vs rolling window) is deliberately confined
    to this one function.
    """
    moment = now if now is not None else datetime.now(UTC)
    if period == "month":
        return moment.strftime("%Y-%m")
    return moment.strftime("%Y-%m-%d")
