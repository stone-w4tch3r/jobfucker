"""Cover-letter generation stage (Phase 5A, task 5.4).

For each (optionally offset-selected) vacancy **above the score threshold**
and not otherwise ineligible, render the apply prompt (resume + vacancy
injected), ask the :class:`AiClient` to generate the cover-letter
body, and persist it as ``cover_letter``. A per-item failure is stored as
``cover_letter_error``; it never aborts the batch.

Ineligible vacancies are **skipped, not failed**: those below the threshold,
``manual_skip=1``, already holding a cover letter, or already in a terminal
apply state (``applied``/``skipped``/``error``).

This is a callable, typer-free stage the Phase 5B engine controller invokes.
Note this stage only **creates the cover-letter text**; the apply *sending* via
the client is the later Part B ``stages/apply.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final

from rusty_results.prelude import Ok, Result

from jobfucker.ai import AiClient
from jobfucker.clients.base import ServiceVacancyId
from jobfucker.reporting import NullReporter, Reporter, RunEvent
from jobfucker.stages.prompts import PromptInputs, VacancyPromptData, render_apply_prompt
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, VacancyRecord

__all__ = ["GenerateCvReport", "GeneratedCv", "run_generate_cv"]

# A vacancy that already reached a terminal apply state is "already final"
# and must not get a fresh cover letter.
_TERMINAL_APPLY_STATES: Final[tuple[str, ...]] = ("applied", "skipped", "error")


def _now() -> str:
    """A SQLite-style UTC timestamp for ``generated_at`` (DDL stores TEXT)."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


async def _select_vacancies(storage: Storage, pipeline_id: int, positions: set[int] | None) -> list[VacancyRecord]:
    """Fetch the vacancies to consider: all, or only the given list offsets.

    ``positions`` are 0-based offsets into the id-ordered ``list_by_pipeline``
    list, computed at run time by the engine's batch selector.
    """
    all_vacancies = await storage.vacancies.list_by_pipeline(pipeline_id)
    if positions is None:
        return all_vacancies
    return [all_vacancies[i] for i in sorted(positions)]


def _is_eligible(vacancy: VacancyRecord, min_required_score: int) -> bool:
    """Whether a vacancy warrants a generated cover letter.

    Ineligible (left untouched): manually skipped, already holding a cover
    letter (idempotency), already in a terminal apply state, or below the
    score threshold (including not-yet-scored / scoring-failed vacancies).
    """
    if vacancy.manual_skip:
        return False
    if vacancy.cover_letter is not None:
        return False  # already generated; don't regenerate on a re-run
    if vacancy.apply_status in _TERMINAL_APPLY_STATES:
        return False  # already final
    if vacancy.score is None:
        return False  # not yet scored / scoring failed
    return vacancy.score >= min_required_score


async def _generate_one(  # noqa: PLR0913 - config-heavy stage: identity + ai + prompt + provenance + threshold
    storage: Storage,
    pipeline: Pipeline,
    ai: AiClient,
    inputs: PromptInputs,
    vacancy: VacancyRecord,
    *,
    snapshot_id: int,
    min_required_score: int,
) -> GeneratedCv:
    """Generate + persist a cover letter for a single vacancy.

    ``min_required_score`` comes from the resolved :class:`PipelineConfig`
    (never the identity DTO), and every write is stamped with
    ``generated_snapshot_id=snapshot_id``.
    """
    if not _is_eligible(vacancy, min_required_score):
        # Ineligible: leave untouched, report as skipped.
        return GeneratedCv(
            external_id=vacancy.external_id,
            cover_letter=vacancy.cover_letter,
            cover_letter_error=vacancy.cover_letter_error,
            skipped=True,
        )

    prompt_data = VacancyPromptData(
        title=vacancy.title,
        company=vacancy.company,
        description=vacancy.description,
        salary=vacancy.salary,
    )
    rendered = render_apply_prompt(
        inputs.prompt,
        resume=inputs.resume,
        vacancy=prompt_data,
    )
    if rendered.is_err:
        updated = replace(
            vacancy,
            cover_letter=None,
            cover_letter_error=rendered.unwrap_err(),
            generated_snapshot_id=snapshot_id,
            generated_at=_now(),
        )
        await storage.vacancies.update(updated)
        return GeneratedCv(
            external_id=vacancy.external_id,
            cover_letter=None,
            cover_letter_error=rendered.unwrap_err(),
            skipped=False,
        )

    result = await ai.generate_cover_letter(rendered.unwrap())
    if result.is_err:
        updated = replace(
            vacancy,
            cover_letter=None,
            cover_letter_error=result.unwrap_err(),
            generated_snapshot_id=snapshot_id,
            generated_at=_now(),
        )
        await storage.vacancies.update(updated)
        return GeneratedCv(
            external_id=vacancy.external_id,
            cover_letter=None,
            cover_letter_error=result.unwrap_err(),
            skipped=False,
        )

    letter = result.unwrap()
    updated = replace(
        vacancy,
        cover_letter=letter,
        cover_letter_error=None,
        generated_snapshot_id=snapshot_id,
        generated_at=_now(),
    )
    await storage.vacancies.update(updated)
    return GeneratedCv(
        external_id=vacancy.external_id,
        cover_letter=letter,
        cover_letter_error=None,
        skipped=False,
    )


async def run_generate_cv(  # noqa: PLR0913 - config-heavy stage: identity + ai + prompt + provenance + threshold
    storage: Storage,
    pipeline: Pipeline,
    ai: AiClient,
    inputs: PromptInputs,
    *,
    snapshot_id: int,
    min_required_score: int,
    positions: set[int] | None = None,
    progress: Reporter | None = None,
) -> Result[GenerateCvReport, str]:
    """Generate cover letters for eligible vacancies and persist them.

    Args:
        storage: the storage facade (four repositories).
        pipeline: the pipeline identity DTO (id for the vacancies/audit).
        ai: the typed AI client (cover-letter generation).
        inputs: the resume contents + parsed apply (cover-letter) prompt.
        snapshot_id: the :class:`PipelineSnapshot` id whose config produced
            this run — stamped ``generated_snapshot_id`` on every cover letter.
        min_required_score: the pass threshold from the resolved config
            (``config.scoring.min_required_score``), never the identity.
        positions: optional set of 0-based offsets into the id-ordered vacancy
            list to consider; ``None`` means all.
        progress: the live output sink (:class:`NullReporter` when omitted) —
            one ``stage='generate_cv'`` event per vacancy. Expected ineligible
            skips are published at ``debug`` level (only ``-vv``), failures and
            successes at ``warning``/``success`` (from no flag / ``-v``).
            Severity→verbosity mapping lives in :mod:`jobfucker.reporting`.

    Returns:
        ``Ok(GenerateCvReport)`` with per-vacancy outcomes. Ineligible vacancies
        are reported as skipped; per-item failures as ``cover_letter_error``.
    """
    reporter = progress if progress is not None else NullReporter()
    vacancies = await _select_vacancies(storage, pipeline.id, positions)
    total = len(vacancies)

    outcomes: list[GeneratedCv] = []
    for position, vacancy in enumerate(vacancies):
        outcome = await _generate_one(
            storage,
            pipeline,
            ai,
            inputs,
            vacancy=vacancy,
            snapshot_id=snapshot_id,
            min_required_score=min_required_score,
        )
        outcomes.append(outcome)
        await reporter.publish(_generate_cv_event(outcome, vacancy, position, total))

    generated = sum(1 for o in outcomes if o.cover_letter is not None and not o.skipped)
    failed = sum(1 for o in outcomes if o.cover_letter_error is not None)
    skipped = sum(1 for o in outcomes if o.skipped)

    details = json.dumps(
        {"total": len(outcomes), "generated": generated, "failed": failed, "skipped": skipped},
        sort_keys=True,
    )
    await storage.audit_log.append(pipeline.id, "generate", details, pipeline_snapshot_id=snapshot_id)

    return Ok(
        GenerateCvReport(
            pipeline_id=pipeline.id,
            total=len(outcomes),
            generated=generated,
            failed=failed,
            skipped=skipped,
            vacancies=tuple(outcomes),
        )
    )


def _generate_cv_event(outcome: GeneratedCv, vacancy: VacancyRecord, position: int, total: int) -> RunEvent:
    """Render one per-vacancy ``generate_cv`` event from a stage outcome."""
    index = position + 1
    if outcome.cover_letter_error is not None:
        return RunEvent(
            stage="generate_cv",
            message=f"cover-letter failed for {vacancy.url}: {outcome.cover_letter_error}",
            level="error",
            index=index,
            total=total,
            item=vacancy.title,
            url=vacancy.url,
        )
    if outcome.skipped:
        # Expected ineligibility (below threshold / not-yet-scored / manual-skip)
        # is situational debug detail — only a ``-vv`` consumer prints it.
        return RunEvent(
            stage="generate_cv",
            message=f"cover letter skipped for {vacancy.url} (ineligible)",
            level="debug",
            index=index,
            total=total,
            item=vacancy.title,
            url=vacancy.url,
        )
    return RunEvent(
        stage="generate_cv",
        message=f"cover letter ready for {vacancy.url}",
        level="success",
        index=index,
        total=total,
        item=vacancy.title,
        url=vacancy.url,
    )


@dataclass(frozen=True, slots=True)
class GeneratedCv:
    """Per-vacancy outcome of the cover-letter stage."""

    external_id: ServiceVacancyId
    cover_letter: str | None
    cover_letter_error: str | None
    skipped: bool  # True when the vacancy was ineligible and left untouched


@dataclass(frozen=True, slots=True)
class GenerateCvReport:
    """Aggregate outcome of one ``run_generate_cv`` invocation."""

    pipeline_id: int
    total: int
    generated: int  # cover letters created
    failed: int  # per-item cover-letter errors
    skipped: int  # ineligible vacancies left untouched
    vacancies: tuple[GeneratedCv, ...]
