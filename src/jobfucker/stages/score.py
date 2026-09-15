"""AI scoring stage (Phase 5A, task 5.3).

For each (optionally position-selected) vacancy, render the scoring prompt
(Jinja2 body with resume + vacancy injected), ask the :class:`AiClient`
to score it 1..5 via structured output, and persist the result on the vacancy:

- success → store ``score`` (from ``fit_score``) + ``score_reasoning`` (from ``comment``);
- **sub-threshold** (``score < pipeline.min_required_score``) → the score is
  stored the same way; sub-threshold is a derived read (``score`` vs the
  snapshot threshold);
- per-item failure → store ``score_error`` and **never abort the batch**.

``apply_status`` is deliberately **not** written here: it is the apply stage's
exclusive vocabulary (``applied``/``skipped``/``error`` = board-side outcomes).

This is a callable, typer-free stage the Phase 5B engine controller invokes; it
is unit/BDD-tested directly through :class:`Storage` + a stub :class:`AiClient`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from rusty_results.prelude import Ok, Result

from jobfucker.ai import AiClient
from jobfucker.clients.base import ServiceVacancyId
from jobfucker.reporting import NullReporter, Reporter, RunEvent
from jobfucker.stages.prompts import PromptInputs, VacancyPromptData, render_scoring_prompt
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, VacancyRecord

__all__ = ["ScoreReport", "ScoredVacancy", "run_score"]


def _now() -> str:
    """A SQLite-style UTC timestamp for ``scored_at`` (DDL stores TEXT)."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


async def _select_vacancies(storage: Storage, pipeline_id: int, positions: set[int] | None) -> list[VacancyRecord]:
    """Fetch the vacancies to score: all, or only the given list offsets.

    ``positions`` are 0-based offsets into the id-ordered ``list_by_pipeline``
    list, computed at run time by the engine's batch selector.
    """
    all_vacancies = await storage.vacancies.list_by_pipeline(pipeline_id)
    if positions is None:
        return all_vacancies
    return [all_vacancies[i] for i in sorted(positions)]


async def _persist_score_error(storage: Storage, vacancy: VacancyRecord, error: str, snapshot_id: int) -> ScoredVacancy:
    """Record a per-item scoring failure and persist it (never abort the batch)."""
    updated = replace(
        vacancy,
        score=None,
        score_reasoning=None,
        score_error=error,
        scored_snapshot_id=snapshot_id,
        scored_at=_now(),
    )
    await storage.vacancies.update(updated)
    return ScoredVacancy(
        external_id=vacancy.external_id,
        score=None,
        score_error=error,
    )


async def _run_score_one(  # noqa: PLR0913 - config-heavy stage: identity + ai + prompt + provenance + snapshot
    storage: Storage,
    pipeline: Pipeline,
    ai: AiClient,
    inputs: PromptInputs,
    vacancy: VacancyRecord,
    *,
    snapshot_id: int,
) -> ScoredVacancy:
    """Score a single vacancy and persist the outcome.

    Rendering failures, AI call failures, and out-of-range scores all collapse
    into a per-vacancy ``score_error`` (handled by the AI wrapper / this stage)
    rather than aborting the batch. The write is stamped with
    ``scored_snapshot_id=snapshot_id``. ``apply_status`` is never touched:
    it counts board-side apply outcomes only (see module docstring).
    """
    prompt_data = VacancyPromptData(
        title=vacancy.title,
        company=vacancy.company,
        description=vacancy.description,
        salary=vacancy.salary,
    )
    rendered = render_scoring_prompt(inputs.prompt, resume=inputs.resume, vacancy=prompt_data)
    if rendered.is_err:
        return await _persist_score_error(storage, vacancy, rendered.unwrap_err(), snapshot_id)

    result = await ai.score(rendered.unwrap())
    if result.is_err:
        return await _persist_score_error(storage, vacancy, result.unwrap_err().message, snapshot_id)

    score_result = result.unwrap()
    # Sub-threshold is a derived property of ``score`` vs the snapshot's
    # threshold, not a stored state: persist the score the same way in both
    # cases. ``apply_status``/``applied_snapshot_id`` are the apply stage's
    # fields and are never touched here — a previous apply decision (or its
    # absence) survives a re-score verbatim.
    updated = replace(
        vacancy,
        score=score_result.fit_score,
        score_reasoning=score_result.comment,
        score_error=None,
        scored_snapshot_id=snapshot_id,
        scored_at=_now(),
    )
    await storage.vacancies.update(updated)
    return ScoredVacancy(
        external_id=vacancy.external_id,
        score=score_result.fit_score,
        score_error=None,
    )


async def run_score(  # noqa: PLR0913 - config-heavy stage: identity + ai + prompt + provenance + threshold
    storage: Storage,
    pipeline: Pipeline,
    ai: AiClient,
    inputs: PromptInputs,
    *,
    snapshot_id: int,
    min_required_score: int,
    positions: set[int] | None = None,
    progress: Reporter | None = None,
) -> Result[ScoreReport, str]:
    """Score the pipeline's vacancies and persist per-vacancy outcomes.

    Args:
        storage: the storage facade (four repositories).
        pipeline: the pipeline identity DTO (id for the vacancies/audit).
        ai: the typed AI client (scoring via structured output).
        inputs: the resume contents + parsed scoring prompt template.
        snapshot_id: the :class:`PipelineSnapshot` id whose config produced
            this run — stamped ``scored_snapshot_id`` on every vacancy scored.
        min_required_score: the pass threshold, from the resolved config
            (``config.scoring.min_required_score``), never the identity.
        positions: optional set of 0-based offsets into the id-ordered vacancy
            list to score; ``None`` scores all.
        progress: the live output sink (:class:`NullReporter` when omitted) —
            one ``stage='score'`` event is published per vacancy as it is scored.

    Returns:
        ``Ok(ScoreReport)`` with per-vacancy outcomes; a whole-batch ``Err`` is
        not produced for normal runs (per-item failures are stored as fields).
    """
    reporter = progress if progress is not None else NullReporter()
    vacancies = await _select_vacancies(storage, pipeline.id, positions)
    total = len(vacancies)

    outcomes: list[ScoredVacancy] = []
    for position, vacancy in enumerate(vacancies):
        await reporter.publish(_score_event_start(vacancy, position, total))
        outcome = await _run_score_one(
            storage,
            pipeline,
            ai,
            inputs,
            vacancy=vacancy,
            snapshot_id=snapshot_id,
        )
        outcomes.append(outcome)
        await reporter.publish(_score_event_finish(outcome, vacancy, position, total, min_required_score))

    scored = sum(1 for o in outcomes if o.score is not None and o.score_error is None)
    sub_threshold = sum(
        1 for o in outcomes if o.score is not None and o.score_error is None and o.score < min_required_score
    )
    failed = sum(1 for o in outcomes if o.score_error is not None)

    details = json.dumps(
        {"total": len(outcomes), "scored": scored, "sub_threshold": sub_threshold, "failed": failed},
        sort_keys=True,
    )
    await storage.audit_log.append(pipeline.id, "score", details, pipeline_snapshot_id=snapshot_id)

    return Ok(
        ScoreReport(
            pipeline_id=pipeline.id,
            total=len(outcomes),
            scored=scored,
            sub_threshold=sub_threshold,
            failed=failed,
            vacancies=tuple(outcomes),
        )
    )


def _score_event_start(vacancy: VacancyRecord, position: int, total: int) -> RunEvent:
    """Render one per-vacancy ``score`` start event.

    The message carries the vacancy URL so a ``-v`` CLI prints e.g. "scored
    https://... with 4/5" and the GUI can hyperlink it (``url`` field).
    """
    index = position + 1
    return RunEvent(
        stage="score",
        message=f"starting scoring {vacancy.url}",
        level="info",
        index=index,
        total=total,
        item=vacancy.title,
        url=vacancy.url,
    )


def _score_event_finish(
    outcome: ScoredVacancy, vacancy: VacancyRecord, position: int, total: int, min_required_score: int
) -> RunEvent:
    """Render one per-vacancy ``score`` event from a scoring outcome.

    The message carries the vacancy URL so a ``-v`` CLI prints e.g. "scored
    https://... with 4/5" and the GUI can hyperlink it (``url`` field).
    Sub-threshold wording is a warning tier: the score is stored, the vacancy
    just sits below the run's threshold.
    """
    index = position + 1
    if outcome.score_error is not None:
        return RunEvent(
            stage="score",
            message=f"score failed for {vacancy.url}: {outcome.score_error}",
            level="error",
            index=index,
            total=total,
            item=vacancy.title,
            url=vacancy.url,
        )
    if outcome.score is not None and outcome.score < min_required_score:
        return RunEvent(
            stage="score",
            message=f"scored {vacancy.url} with {outcome.score}/5 — below threshold, skipped",
            level="warning",
            index=index,
            total=total,
            item=vacancy.title,
            url=vacancy.url,
        )
    return RunEvent(
        stage="score",
        message=f"scored {vacancy.url} with {outcome.score}/5",
        level="success",
        index=index,
        total=total,
        item=vacancy.title,
        url=vacancy.url,
    )


@dataclass(frozen=True, slots=True)
class ScoredVacancy:
    """Per-vacancy outcome of the score stage."""

    external_id: ServiceVacancyId
    score: int | None
    score_error: str | None


@dataclass(frozen=True, slots=True)
class ScoreReport:
    """Aggregate outcome of one ``run_score`` invocation."""

    pipeline_id: int
    total: int
    scored: int  # vacancies successfully scored (any threshold)
    sub_threshold: int  # scored below min_required_score (derived, not a stored state)
    failed: int  # per-item scoring errors
    vacancies: tuple[ScoredVacancy, ...]
