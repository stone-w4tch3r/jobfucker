"""Apply stage (Phase 5B, task 5.5).

Applies to each **eligible** vacancy (scored at/above ``min_required_score``,
holds a generated cover letter, not manual-skip, and not already
decided) by calling ``client.apply_to_vacancy(resume_id, vacancy_id,
message=cover_letter)``.

Eligibility is relaxable per run through :class:`ApplyFilters`:

- ``min_score`` overrides the config threshold for this run only.
- ``include_unscored`` makes vacancies without a score eligible.
- ``allow_without_letter`` makes letterless vacancies eligible; the client is
  then called with ``message=None`` (the contract supports letterless apply).
- ``only_external_ids`` restricts the run to exactly these board vacancy ids
  and bypasses the manual-skip/score/letter filters for them (the decided
  gate stays); ``force_decided`` (only meaningful with ids) bypasses the
  decided gate too, enabling re-attempts of previously decided vacancies.
Daily-limit enforcement applies in every mode.

Ineligible vacancies are never attempted; every one of them is reported with
its dumb-obvious reason (``ApplyReport.checked``/``ignored`` plus an
``info``-tier ``not eligible <id> — <reason>`` event) so an empty run explains
itself.

**Limit enforcement (before every application):** the **one shared per-auth
counter** for ``(service, login, date)`` (from the resolved config) is checked
via :class:`jobfucker.limits.Limits` against **both** caps — the board's
``service_info.per_auth_daily_cap`` and the pipeline's ``daily_apply_limit``.
Both caps apply to the same shared value, so many pipelines on one account
exhaust that account's single counter (the cap is never double-counted). On a
successful apply that counter is incremented once.

Outcome mapping (respecting the DDL ``apply_status`` CHECK — no ``rejected``):

- ``ApplySucceeded`` → ``apply_status='applied'`` + ``applied_at`` and increment counters.
- ``ApplySkipped`` → ``apply_status='skipped'`` + ``skip_reason``=the board's
  skip text (so the reason survives for later "why ignored" reporting).
- ``ApplyFailed`` → per-vacancy ``apply_status='error'``.
- ordinary ``ClientError`` → per-vacancy ``apply_status='error'`` + ``apply_error``
  (**never aborts the batch**).
- :class:`LimitExceededError` (from the board), :class:`ConfigurationError`
  (e.g. the board rejected the configured resume — fatal by spec, retries
  cannot help), or :class:`AuthError` → **stop applying**: remaining eligible
  vacancies stay ``pending``, and the stage returns an ``Ok`` report with
  ``limit_reached``/``stopped_early`` (never an error).

Idempotent: an already-``applied`` (or otherwise decided) vacancy is never
re-applied to. One ``audit_log`` entry (``action='apply'``) is written.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final, Literal

from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    ApplyFailed,
    ApplyResult,
    ApplySkipped,
    ApplySucceeded,
    AuthError,
    Client,
    ClientError,
    ConfigurationError,
    LimitExceededError,
    ServiceVacancyId,
)
from jobfucker.hh_tests.contract import (
    HhTestCapable,
    HhTestProblem,
    HhTestSolveOutcome,
    HhTestSolver,
    HhTestUnsolved,
)
from jobfucker.hh_tests.prompt import render_hh_test_prompt
from jobfucker.limits import Limits, today_iso
from jobfucker.reporting import NullReporter, Reporter, RunEvent
from jobfucker.stages.prompts import PromptInputs, VacancyPromptData
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import ApplyStatus, Pipeline, VacancyRecord

__all__ = ["AppliedVacancy", "ApplyFilters", "ApplyReport", "ApplyTargets", "run_apply"]

logger = logging.getLogger(__name__)

# A vacancy that has already reached a decided outcome is never re-applied to
# (idempotency); only untouched/pending vacancies are candidates.
_DECIDED: Final[tuple[str, ...]] = ("applied", "skipped", "error")

# Display verb per stored apply status (total over ``ApplyStatus`` so the
# lookup never fails; only decided statuses reach it). "declined"/"failed"
# separate board refusals and errors from the omission sense of "skipped".
_DECIDED_VERBS: Final[dict[ApplyStatus, str]] = {
    "pending": "pending",
    "applied": "applied",
    "skipped": "declined",
    "error": "failed",
}


@dataclass(frozen=True, slots=True)
class ApplyFilters:
    """Per-run eligibility overrides for the apply stage (CLI-facing knobs).

    Every field defaults to the strict automatic behavior: the config
    threshold, unscored vacancies excluded, a cover letter required, all
    vacancies considered, and decided vacancies never re-attempted.

    ``has_hh_test`` narrows the selection to test-bearing / testless rows using
    the fetch-time ``has_hh_test`` flag (a selection filter over the loaded
    rows, no per-vacancy probing): ``any`` is the default,
    ``only_with_hh_tests`` keeps rows where the flag is true,
    ``only_without_hh_tests`` keeps rows where it is false-or-unknown.

    ``only_external_ids`` both **restricts the selection** to exactly these
    board vacancy ids and bypasses the manual-skip/score/letter filters for
    them; the decided gate still applies unless ``force_decided`` is set
    (only meaningful together with ids). Daily limits apply in every mode.
    """

    min_score: int | None = None  # None -> the config threshold (``min_required_score``)
    include_unscored: bool = False
    allow_without_letter: bool = False
    only_external_ids: frozenset[str] = frozenset()
    force_decided: bool = False
    has_hh_test: Literal["any", "only_with_hh_tests", "only_without_hh_tests"] = "any"

    def __post_init__(self) -> None:
        # Domain invariant (not just a CLI rule): force_decided without ids
        # would re-attempt every decided vacancy in the pipeline.
        if self.force_decided and not self.only_external_ids:
            raise ValueError("force_decided requires only_external_ids")


# The shared strict default (never mutated): B008-safe default for ``run_apply``.
_STRICT_FILTERS: Final[ApplyFilters] = ApplyFilters()


@dataclass(frozen=True, slots=True)
class ApplyTargets:
    """The client + resolved service resume id the apply stage acts through.

    Both are resolved by the engine controller from the pipeline config +
    client factory (never hardcoded in core).
    """

    client: Client
    resume_id: str


@dataclass(frozen=True, slots=True)
class AppliedVacancy:
    """Per-vacancy outcome of the apply stage."""

    external_id: ServiceVacancyId
    status: ApplyStatus
    message: str | None
    pending: bool = False  # True when this candidate was left pending (limit stop)


@dataclass(frozen=True, slots=True)
class IgnoredVacancy:
    """A vacancy the stage will not attempt, with the reason it was skipped.

    ``reason`` is the single human-readable gate the vacancy failed (exactly
    one per vacancy — the root-cause gate, checked in the order the user can
    act on: manual-skip → prior decision → scoring → cover letter).
    """

    external_id: ServiceVacancyId
    title: str
    url: str
    reason: str


@dataclass(frozen=True, slots=True)
class ApplyReport:
    """Aggregate outcome of one ``run_apply`` invocation."""

    pipeline_id: int
    checked: int  # all non-deleted vacancies in the selection (before eligibility)
    total: int  # eligible candidates considered
    applied: int
    skipped: int
    failed: int
    pending: int  # eligible candidates left untouched (incl. a limit stop)
    limit_reached: bool  # True when a counter or the board signalled stop
    stopped_early: bool  # True when further applications were stopped early
    stop_message: str | None  # the stop reason wording; None when nothing stopped
    vacancies: tuple[AppliedVacancy, ...]
    ignored: tuple[IgnoredVacancy, ...]


def _now() -> str:
    """A SQLite-style UTC timestamp for ``applied_at`` (DDL stores TEXT)."""
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


async def _select_candidates(
    storage: Storage,
    pipeline: Pipeline,
    positions: set[int] | None,
    only_external_ids: frozenset[str],
    has_hh_test: Literal["any", "only_with_hh_tests", "only_without_hh_tests"],
) -> list[VacancyRecord]:
    """Vacancies to consider: all, only the given list offsets, or only the given ids.

    ``positions`` are 0-based offsets into the id-ordered ``list_by_pipeline``
    list, computed at run time by the engine's batch selector. Non-empty
    ``only_external_ids`` (the forced-ids mode) selects exactly those board
    vacancy ids in stored id order and takes precedence; the engine has
    already fail-fast on unknown ids.

    ``has_hh_test`` narrows by the fetch-time flag: ``only_with_hh_tests``
    keeps rows flagged true, ``only_without_hh_tests`` keeps rows flagged
    false-or-NULL (a board without the notion reads as "no test"). The filter
    applies to the forced-ids selection too, so ``--vacancy-id`` combined with
    ``--has-hh-tests`` never silently ignores the flag.
    """
    all_vacancies = await storage.vacancies.list_by_pipeline(pipeline.id)
    if only_external_ids:
        selected = [v for v in all_vacancies if v.external_id in only_external_ids]
    else:
        selected = all_vacancies if positions is None else [all_vacancies[i] for i in sorted(positions)]
    if has_hh_test == "only_with_hh_tests":
        return [v for v in selected if v.has_hh_test is True]
    if has_hh_test == "only_without_hh_tests":
        return [v for v in selected if v.has_hh_test is not True]
    return selected


def _decided_reason(vacancy: VacancyRecord) -> str:
    """The dumb reason for a vacancy that already reached a decided outcome.

    The detail column is per status: the board's skip text (``skip_reason``)
    for ``skipped``, the error text (``apply_error``) for ``error``. A missing
    detail means the decision was written without a board message (or edited
    manually). Verbs match the display vocabulary: applied/declined/failed —
    never the DB noun ``skipped``.
    """
    status = vacancy.apply_status
    assert status is not None  # decided gate guarantees a status
    detail_source = vacancy.skip_reason if status == "skipped" else vacancy.apply_error
    verb = _DECIDED_VERBS[status]
    detail = f": {detail_source}" if detail_source else ""
    return f"{verb} in an earlier run{detail}"


def _ignore_reason(  # noqa: PLR0911 - one return per eligibility gate (distinct user-facing reason)
    vacancy: VacancyRecord, min_required_score: int, filters: ApplyFilters
) -> str | None:
    """Why a vacancy is NOT an apply candidate; ``None`` when it is one.

    Gates are checked root-cause-first so the reported reason is the one the
    user can act on: manual-skip → prior decision → scoring → cover letter.

    A forced vacancy (its id is in ``only_external_ids``) skips the
    manual-skip/score/letter filters entirely — the caller explicitly named
    it — and only the decided gate remains (bypassed by ``force_decided``).

    The decided gate is checked **before** the score gate: a decided vacancy is
    decided regardless of its score, and an unscored-but-already-decided row
    must not slip through under ``--include-unscored`` (which returns early on
    the score gate) and be re-attempted.
    """
    forced = vacancy.external_id in filters.only_external_ids
    decided = vacancy.apply_status is not None and vacancy.apply_status in _DECIDED and not filters.force_decided
    if not forced:
        if vacancy.manual_skip:
            detail = f": {vacancy.manual_skip_reason}" if vacancy.manual_skip_reason else ""
            return f"manually skipped{detail}"
        if decided:
            return _decided_reason(vacancy)
        if vacancy.score is None:
            return None if filters.include_unscored else "not scored yet (run score first)"
        threshold = filters.min_score if filters.min_score is not None else min_required_score
        if vacancy.score < threshold:
            return f"score {vacancy.score} < required {threshold}"
    if decided:
        return _decided_reason(vacancy)
    if not forced and vacancy.cover_letter is None and not filters.allow_without_letter:
        return "no cover letter (run generate first)"
    return None


@dataclass(frozen=True, slots=True)
class _ApplyContext:
    """Shared state threaded through one ``run_apply`` invocation.

    ``service``/``login`` are the auth identity from the resolved config — the
    ``(service, login, date)`` key of the **single shared** daily counter every
    pipeline on the account reads and increments (never double-counted).

    ``hh_test_solver``/``hh_test_prompt`` are the optional HH screening-test
    solving seam: when both the solver and a capable client are present, a
    test-bearing (``has_hh_test``) vacancy routes through fetch → solve → web
    apply; otherwise it applies plainly (or skips via the board's own
    ``test_required`` signal).
    """

    storage: Storage
    pipeline: Pipeline
    targets: ApplyTargets
    day: str
    service: str
    login: str
    snapshot_id: int
    limits: Limits
    hh_test_solver: HhTestSolver | None = None
    hh_test_prompt: PromptInputs | None = None


@dataclass(frozen=True, slots=True)
class _ApplyOutcome:
    """Result of attempting one application (with the shared-counter delta)."""

    status: ApplyStatus
    message: str | None
    delta: int


async def select_candidates(
    storage: Storage,
    pipeline: Pipeline,
    min_required_score: int,
    *,
    positions: set[int] | None,
    filters: ApplyFilters,
) -> tuple[list[VacancyRecord], list[IgnoredVacancy]]:
    """Split a selection into eligible candidates and ineligibility-rejected rows.

    The shared selection used by the apply stage and the ``hh-tests dump``
    command so both operate on exactly the same universe. Only the raw split —
    event publishing is stage-specific.
    """
    all_vacancies = await _select_candidates(
        storage, pipeline, positions, filters.only_external_ids, filters.has_hh_test
    )
    candidates: list[VacancyRecord] = []
    ignored: list[IgnoredVacancy] = []
    for vacancy in all_vacancies:
        reason = _ignore_reason(vacancy, min_required_score, filters)
        if reason is None:
            candidates.append(vacancy)
            continue
        ignored.append(IgnoredVacancy(vacancy.external_id, vacancy.title, vacancy.url, reason))
    return candidates, ignored


async def _apply_one(ctx: _ApplyContext, vacancy: VacancyRecord, used: int) -> _ApplyOutcome:
    """Apply to one vacancy and persist the outcome.

    Both dual caps (per-auth from ``service_info``, per-pipeline from config)
    are enforced against the ONE shared auth counter ``used`` — the effective
    stop is the tighter of the two. A ``"pending"`` outcome (with a stop)
    covers a pre-apply counter check failing, the board returning
    :class:`LimitExceededError`, and fatal errors the spec says must stop the
    pipeline (:class:`ConfigurationError`, :class:`AuthError`) — in each case
    the batch halts.

    A test-bearing vacancy under an HH-capable client + a wired solver routes
    through fetch → solve → ``apply_to_vacancy_with_test`` first; the plain
    API apply is used for everything else (and as the fallback when the fresh
    test page no longer carries the test).
    """
    if not ctx.limits.can_apply(used, used):
        return _ApplyOutcome("pending", None, delta=0)

    if isinstance(ctx.targets.client, HhTestCapable) and vacancy.has_hh_test is True:
        test_outcome = await _apply_hh_test(ctx, vacancy, ctx.targets.client)
        if test_outcome is not None:
            return test_outcome

    result = await ctx.targets.client.apply_to_vacancy(
        resume_id=ctx.targets.resume_id,
        vacancy_id=vacancy.external_id,
        message=vacancy.cover_letter,
    )
    return await _classify_apply_result(ctx, vacancy, result)


async def _apply_hh_test(  # noqa: PLR0911 - one return per distinct test-path outcome (fetch/stop/no-test/no-solver/solve-fail/decline/apply)
    ctx: _ApplyContext, vacancy: VacancyRecord, client: HhTestCapable
) -> _ApplyOutcome | None:
    """The HH screening-test path;

    Only engages when the client is :class:`HhTestCapable` (the ``isinstance``
    capability gate — mock/other boards excluded by construction), a solver is
    wired, and the fetch-time ``has_hh_test`` flag says the vacancy is
    test-bearing. A fetch/solve/render failure is a per-vacancy **failed**
    outcome (``apply_error``), never a batch abort. ``get_vacancy_test``
    returning ``None`` (test removed since fetch) falls through to the plain
    apply via ``None``.
    """

    # Probe before deciding anything about solvers: the fetch-time flag can be
    # stale (the employer removed the test since), and a fresh "no test" page
    # must self-heal to the plain apply rather than fail for a missing solver.
    problem_result = await client.get_vacancy_test(vacancy.external_id)
    if problem_result.is_err:
        error = problem_result.unwrap_err()
        # Auth/configuration/board-limit failures are account-wide, not this
        # vacancy's fault: stop the batch exactly like the plain apply path
        # instead of marking every test-bearing vacancy permanently `error`.
        stop = _get_batch_stop_message_if_batch_stop_needed(error)
        if stop is not None:
            return _ApplyOutcome("pending", stop, delta=0)
        return await _persist_failed_per_vacancy(ctx, vacancy, f"test fetch failed: {error.message}")
    problem = problem_result.unwrap()
    if problem is None:
        return None

    solver = ctx.hh_test_solver
    if solver is None:
        # A live test on a capable board with no solver wired cannot be
        # answered: surface it as a per-vacancy failure (the plan's semantics)
        # rather than letting the board decline it later.
        return await _persist_failed_per_vacancy(
            ctx,
            vacancy,
            "test present but no solver configured — enable hh_test_solving or pass --test-answers",
        )

    solution_result = await _solve_problem(ctx, vacancy, problem, solver)
    if solution_result.is_err:
        return await _persist_failed_per_vacancy(ctx, vacancy, solution_result.unwrap_err())
    outcome = solution_result.unwrap()
    if isinstance(outcome, HhTestUnsolved):
        # The AI declined with a comment: a skip, not a failure. Stored as
        # ``skipped`` so the reason survives, and surfaced through the normal
        # per-vacancy reporter line (``declined <id> — <comment>``).
        logger.info("HH test for %s skipped by AI: %s", vacancy.external_id, outcome.comment)
        return await _persist_skipped_per_vacancy(ctx, vacancy, outcome.comment)

    result = await client.apply_to_vacancy_with_test(
        resume_id=ctx.targets.resume_id,
        vacancy_id=vacancy.external_id,
        message=vacancy.cover_letter,
        solution=outcome,
    )
    return await _classify_apply_result(ctx, vacancy, result)


async def _solve_problem(
    ctx: _ApplyContext,
    vacancy: VacancyRecord,
    problem: HhTestProblem,
    solver: HhTestSolver,
) -> Result[HhTestSolveOutcome, str]:
    """Render the test prompt (when one is configured) and solve the whole test.

    The user-authored prompt is the solver's context (resume + vacancy + the
    formatted test); a file-backed solver ignores the rendered text, so a
    missing prompt section yields an empty context rather than an error.
    """
    prompt = ""
    inputs = ctx.hh_test_prompt
    if inputs is not None:
        rendered = render_hh_test_prompt(
            inputs.prompt,
            resume=inputs.resume,
            vacancy=VacancyPromptData(
                title=vacancy.title,
                company=vacancy.company,
                description=vacancy.description,
                salary=vacancy.salary,
            ),
            test=problem,
        )
        if rendered.is_err:
            return Err(rendered.unwrap_err())
        prompt = rendered.unwrap()
    return await solver(problem, prompt)


async def _persist_failed_per_vacancy(ctx: _ApplyContext, vacancy: VacancyRecord, message: str) -> _ApplyOutcome:
    """Record a per-vacancy apply failure (never aborts the batch)."""
    updated = replace(
        vacancy,
        apply_status="error",
        skip_reason=None,
        apply_error=message,
        applied_at=None,
        applied_snapshot_id=ctx.snapshot_id,
    )
    await ctx.storage.vacancies.update(updated)
    return _ApplyOutcome("error", message, delta=0)


async def _persist_skipped_per_vacancy(ctx: _ApplyContext, vacancy: VacancyRecord, reason: str) -> _ApplyOutcome:
    """Record a per-vacancy skip (AI declined the test); never aborts the batch.

    ``apply_status='skipped'`` + ``skip_reason=reason``, mirroring the board's
    own ``ApplySkipped`` mapping, so the AI comment is preserved for later
    "why ignored" reporting and printed as the ``declined`` per-vacancy line.
    """
    updated = replace(
        vacancy,
        apply_status="skipped",
        skip_reason=reason,
        apply_error=None,
        applied_at=None,
        applied_snapshot_id=ctx.snapshot_id,
    )
    await ctx.storage.vacancies.update(updated)
    return _ApplyOutcome("skipped", reason, delta=0)


def _get_batch_stop_message_if_batch_stop_needed(error: ClientError) -> str | None:
    """The batch-stop wording for a fatal client error, or ``None`` (per-vacancy).

    Account-wide failures must halt the run rather than decide this one vacancy:
    the board's own cap gets a fixed readable line, the fatal errors (auth,
    configuration — retries cannot help) carry their own message.
    """
    if isinstance(error, LimitExceededError):
        return "board signalled the daily limit"
    if isinstance(error, (ConfigurationError, AuthError)):
        return error.message
    return None


async def _classify_apply_result(
    ctx: _ApplyContext,
    vacancy: VacancyRecord,
    result: Result[ApplyResult, ClientError],
) -> _ApplyOutcome:
    """Classify a client apply result into the persisted outcome (test or plain)."""
    if result.is_err:
        error = result.unwrap_err()
        stop = _get_batch_stop_message_if_batch_stop_needed(error)
        if stop is not None:
            return _ApplyOutcome("pending", stop, delta=0)
        return await _persist_failed_per_vacancy(ctx, vacancy, error.message)

    application = result.unwrap()
    match application:
        case ApplySucceeded():
            await ctx.storage.daily_limits.increment(ctx.service, ctx.login, ctx.day)
            updated = replace(
                vacancy,
                apply_status="applied",
                skip_reason=None,
                apply_error=None,
                applied_at=_now(),
                applied_snapshot_id=ctx.snapshot_id,
            )
            await ctx.storage.vacancies.update(updated)
            return _ApplyOutcome("applied", None, delta=1)
        case ApplySkipped(skip=skip):
            return await _persist_skipped_per_vacancy(ctx, vacancy, skip.text)
        case ApplyFailed(error=error):
            updated = replace(
                vacancy,
                apply_status="error",
                skip_reason=None,
                apply_error=error.text,
                applied_at=None,
                applied_snapshot_id=ctx.snapshot_id,
            )
            await ctx.storage.vacancies.update(updated)
            return _ApplyOutcome("error", error.text, delta=0)


async def run_apply(  # noqa: PLR0913 - config-heavy stage: identity + targets + provenance + thresholds + auth key
    storage: Storage,
    pipeline: Pipeline,
    targets: ApplyTargets,
    *,
    snapshot_id: int,
    min_required_score: int,
    daily_apply_limit: int,
    login: str,
    service: str,
    filters: ApplyFilters = _STRICT_FILTERS,
    positions: set[int] | None = None,
    today: str | None = None,
    progress: Reporter | None = None,
    hh_test_solver: HhTestSolver | None = None,
    hh_test_prompt: PromptInputs | None = None,
) -> Result[ApplyReport, str]:
    """Apply to eligible vacancies, enforcing the shared daily limit.

    Args:
        storage: the storage facade (four repositories).
        pipeline: the pipeline identity DTO (id for vacancies/audit).
        targets: the resolved client + service resume id.
        snapshot_id: the :class:`PipelineSnapshot` id whose config produced
            this run — stamped ``applied_snapshot_id`` on every vacancy the
            apply stage decides.
        min_required_score: the pass threshold from the resolved config
            (the default ``filters.min_score`` fallback).
        daily_apply_limit: the pipeline's own cap from the resolved config.
        login: the auth login (account identity) from the resolved config.
        service: the client selector from the resolved config — together with
            ``login`` and ``today`` these key the shared per-auth counter.
        filters: per-run eligibility overrides (:class:`ApplyFilters`; the
            default is the strict automatic behavior).
        positions: optional set of 0-based offsets into the id-ordered vacancy
            list to consider; ``None`` = all (ignored in forced-ids mode).
        today: the ``YYYY-MM-DD`` counter key; defaults to :func:`today_iso`
            (injectable for deterministic tests).
        progress: the live output sink (:class:`NullReporter` when omitted) —
            one ``stage='apply'`` event per vacancy attempted, an ``info``
            ``not eligible <id> — <reason>`` event per ineligible vacancy, plus
            a ``stopping: <reason>`` event when a cap halts the batch.

    Returns:
        ``Ok(ApplyReport)`` — ``checked`` counts every non-deleted vacancy in
        the selection, ``total`` the eligible candidates, ``ignored`` carries
        each rejected vacancy with its reason; plus ``limit_reached``/
        ``stopped_early`` when applying stopped. A whole-batch ``Err`` is not
        produced for normal runs: per-item failures are stored as fields, and a
        limit stop is an ``Ok`` outcome, never an error.
    """
    reporter = progress if progress is not None else NullReporter()
    day = today if today is not None else today_iso()

    # One shared counter per (service, login, date), read once up front. Both
    # dual caps (per-auth from the board, per-pipeline from config) apply to it:
    # pipelines on the same account share it, so the cap is never double-counted.
    per_auth_cap = targets.client.service_info.per_auth_daily_cap
    limits = Limits(per_auth_cap=per_auth_cap, per_pipeline_limit=daily_apply_limit)
    limit_row = await storage.daily_limits.get(service, login, day)
    used = limit_row.count if limit_row is not None else 0

    candidates, ignored = await select_candidates(
        storage, pipeline, min_required_score, positions=positions, filters=filters
    )
    for ignored_vacancy in ignored:
        await reporter.publish(
            RunEvent(
                stage="apply",
                message=f"not eligible {ignored_vacancy.external_id} — {ignored_vacancy.reason}",
                level="info",
                item=ignored_vacancy.title,
                url=ignored_vacancy.url,
            )
        )

    ctx = _ApplyContext(
        storage=storage,
        pipeline=pipeline,
        targets=targets,
        day=day,
        service=service,
        login=login,
        snapshot_id=snapshot_id,
        limits=limits,
        hh_test_solver=hh_test_solver,
        hh_test_prompt=hh_test_prompt,
    )

    outcomes: list[AppliedVacancy] = []
    applied = 0
    skipped = 0
    failed = 0
    stopped_early = False
    stop_message: str | None = None
    total = len(candidates)

    for position, vacancy in enumerate(candidates):
        outcome = await _apply_one(ctx, vacancy, used)
        index = position + 1
        if outcome.status == "pending":
            stopped_early = True
            # A None message is exactly the shared-counter stop (can_apply just
            # failed), so the tighter cap's wording is always available there.
            stop_message = outcome.message if outcome.message is not None else ctx.limits.stop_reason(used)
            assert stop_message is not None  # counter stop ⇒ stop_reason() has a bound cap
            await reporter.publish(
                RunEvent(
                    stage="apply",
                    # No index/total: a stop is not an attempt, so the CLI
                    # prints it bare (no [i/n] position frame).
                    message=f"stopping: {stop_message}",
                    level="warning",
                    item=vacancy.title,
                    url=vacancy.url,
                )
            )
            break
        used += outcome.delta
        outcomes.append(AppliedVacancy(vacancy.external_id, outcome.status, outcome.message))
        await reporter.publish(_apply_event(outcome.status, outcome.message, vacancy, index, total))
        if outcome.status == "applied":
            applied += 1
        elif outcome.status == "skipped":
            skipped += 1
        else:
            failed += 1

    pending = len(candidates) - applied - skipped - failed
    limit_reached = stopped_early

    details = json.dumps(
        {
            "total": len(candidates),
            "applied": applied,
            "skipped": skipped,
            "failed": failed,
            "pending": pending,
            "limit_reached": limit_reached,
        },
        sort_keys=True,
    )
    await storage.audit_log.append(pipeline.id, "apply", details, pipeline_snapshot_id=snapshot_id)

    return Ok(
        ApplyReport(
            pipeline_id=pipeline.id,
            checked=len(candidates) + len(ignored),
            total=len(candidates),
            applied=applied,
            skipped=skipped,
            failed=failed,
            pending=pending,
            limit_reached=limit_reached,
            stopped_early=stopped_early,
            stop_message=stop_message,
            vacancies=tuple(outcomes),
            ignored=tuple(ignored),
        )
    )


def _apply_event(status: ApplyStatus, message: str | None, vacancy: VacancyRecord, index: int, total: int) -> RunEvent:
    """Render one per-vacancy ``apply`` event from an apply outcome.

    Verb-first display vocabulary, id before detail (the CLI adds the
    ``[i/n]`` position prefix): ``applied {id} — {title}`` (success),
    ``declined {id} — {board text}`` (warning), ``failed {id} — {error text}``
    (error). A missing detail drops the em-dash segment.
    """
    if status == "applied":
        return RunEvent(
            stage="apply",
            message=f"applied {vacancy.external_id} — {vacancy.title}",
            level="success",
            index=index,
            total=total,
            item=vacancy.title,
            url=vacancy.url,
        )
    detail = message if message is not None else ""
    if status == "skipped":
        text = f"declined {vacancy.external_id} — {detail}" if detail else f"declined {vacancy.external_id}"
        return RunEvent(
            stage="apply",
            message=text,
            level="warning",
            index=index,
            total=total,
            item=vacancy.title,
            url=vacancy.url,
        )
    text = f"failed {vacancy.external_id} — {detail}" if detail else f"failed {vacancy.external_id}"
    return RunEvent(
        stage="apply",
        message=text,
        level="error",
        index=index,
        total=total,
        item=vacancy.title,
        url=vacancy.url,
    )
