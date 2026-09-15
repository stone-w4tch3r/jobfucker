"""Live run-output stream (UI-agnostic progress + per-item events).

This module is the **shared out-channel** both the core track (engine, stages)
and every client track (clients) publish run events on, and which both
presentation clients (CLI today, Qt GUI later) consume while a stage or client
operation runs. It is **not core-private**: the frozen contract
(``docs/specs/client-contract.md`` §8) points here so both tracks build against
the same ``RunEvent``/``Reporter``/``EventLevel`` types and import them freely
(generic names only — no board identifiers).

It follows the ``building-multi-ui-apps`` interaction-interface pattern: the
core/client decides *what* to report, never *how* events are rendered or shown —
the consumer implements the small :class:`Reporter` protocol with its own idiom:

- **CLI** — an ``echo``-based reporter that prints each event as it arrives,
  filtered by the ``-v``/``-vv`` verbosity level;
- **Qt GUI** — a future RunManager that advances the Workspace progress bar and
  appends to its run log from the same stream (and honours the per-event
  ``index``/``total`` fields for the bar).

**Severity drives verbosity** (the classic pattern, unifying logging levels with
the ``-v`` flag): :func:`verbosity_of` maps an event's ``level`` to the minimum
``-v`` count required to print it — ``success``/``warning``/``error`` always
surface (verbosity ``0``, the no-flag default), ``info`` prints at ``-v``, and
``debug``-level board-operation detail only at ``-vv``. Events carry only the
semantic ``level``; the verbosity is **derived**, never stored, so the CLI and
the Qt GUI filter identically from one shared map.

Every per-vacancy success prints by default (that is the whole point of running
a stage); the ``*Report`` objects additionally give the CLI its final summary
lines regardless of verbosity, and the GUI maps those to run-log summary lines.

The stream is **additive** to the reports: stages still persist + return their
``*Report`` and write ``audit_log`` rows exactly as before; reporting never
changes what a stage *does*, only that it *tells*.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal, Protocol

__all__ = ["EventLevel", "NullReporter", "Reporter", "RunEvent", "verbosity_of"]


EventLevel = Literal["debug", "info", "success", "warning", "error"]

# Severity rank: higher = more important. Used by verbosity_of and by a GUI to
# order/badge event rows. success/warning/error all surface by default;
# info is the -v tier; debug is the quietest.
_SEVERITY_RANK: Final[Mapping[EventLevel, int]] = {
    "debug": 0,
    "info": 1,
    "success": 2,
    "warning": 3,
    "error": 4,
}


def verbosity_of(level: EventLevel) -> int:
    """The minimum ``-v`` count a consumer must use to print ``level`` events.

    Classic severity→verbosity mapping: successes, warnings and errors always
    surface (verbosity ``0``, the no-flag default) — a successful per-vacancy
    apply/score/cover-letter is exactly what the user runs the stage to see;
    ``info``-level events print at ``-v`` (1); debug-level board-operation
    detail only shows at ``-vv`` (2). Keeping this map in ONE module
    guarantees the CLI and the Qt GUI filter identically.
    """
    if _SEVERITY_RANK[level] >= _SEVERITY_RANK["success"]:
        return 0
    if level == "debug":
        return 2
    return 1


@dataclass(frozen=True, slots=True)
class RunEvent:
    """One live progress/outcome event emitted during a run.

    Field semantics:

    - ``stage`` — the logical origin tag (``fetch``/``score``/``generate_cv``/
      ``apply`` for core stages; ``authorize``/``search``/``resumes``/``apply``/
      ``captcha`` for client operations). Used by the UI to keep the run log
      grouped and by the CLI as a line prefix.
    - ``index``/``total`` — 1-based item position within the stage batch and its
      size, when known up-front (position stages). ``None`` when the stage
      cannot know the total before finishing (fetch pages results live).
    - ``item`` — a single display label for the item (title or external id).
    - ``url`` — the vacancy URL when the event concerns one (lets the GUI
      hyperlink; the human message is still self-contained for the CLI).
    - ``message`` — the fully-rendered human line (e.g. ``"applied to
      https://hh.ru/vacancy/123"``).
    - ``level`` — the semantic severity (:func:`verbosity_of` derives the
      verbosity at which a consumer prints the event).
    """

    stage: str
    message: str
    level: EventLevel = "info"
    index: int | None = None
    total: int | None = None
    item: str | None = None
    url: str | None = None


class Reporter(Protocol):
    """The out-channel the core stages + clients emit live events on.

    Implementations are UI-idiom-specific: the CLI prints (filtering by a
    verbosity threshold), the Qt GUI will emit Qt signals from a manager. An
    event may be dropped by a consumer — reporters never raise.
    """

    async def publish(self, event: RunEvent) -> None:
        """Deliver one run-event to the consumer (may be dropped)."""
        ...


class NullReporter:
    """The default no-op sink: emits nothing, never fails.

    Plain (non-``Protocol``) so the ``Engine``/stage defaults can instantiate a
    concrete object; satisfies :class:`Reporter`.
    """

    async def publish(self, event: RunEvent) -> None:
        """Discard the event (the no-op sink)."""
        del event
