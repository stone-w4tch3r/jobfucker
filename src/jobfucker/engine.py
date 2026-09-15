"""Callable engine core: per-stage methods + ``run`` composition (Phase 5B, task 5.1).

This is the **typer-free** pipeline orchestrator. It exposes one distinct
method per stage — each with its own concrete report — plus :meth:`Engine.run`
to compose them:

- :meth:`Engine.fetch` — run the fetch stage for a window/slice of the
  listing (**takes no position-batch selector**);
- :meth:`Engine.score` / :meth:`Engine.generate_cv` / :meth:`Engine.apply` —
  the position stages, each taking a batch selector;
- :meth:`Engine.run` — compose ``fetch → score → generate_cv → apply``.

It encapsulates the **batch semantics** the CLI/GUI share:

- ``from_``/``to``/``take`` map to 0-based **offsets into the id-ordered
  vacancy list**, computed at run time from ``list_by_pipeline`` (fetch keeps
  its board-listing window flags unchanged). A ``--to`` combined with `--take`
  is a validation error; `take` without `from` starts at offset 0; `to` is
  inclusive.
- ``skip_already_processed`` is **stage-relative**: it excludes vacancies
  that already carry *this stage's* result — a score for ``score``, a cover
  letter for ``generate``, an apply decision for ``apply``. ``apply_status``
  is the apply stage's exclusive vocabulary (the score stage never stamps
  it), so a scored-but-letterless vacancy is still a candidate for
  ``generate``, and a scored-but-never-applied one for ``apply``. This is
  stage-level idempotency only: ``fetch`` is an insert-only mirror sync, so
  it never wipes prior processing results.
- **Per-item failures are field values, not batch aborts** (the stages persist
  per-vacancy errors; the engine never stops the batch for one bad item).

Clients/resume_id are resolved from the pipeline config + client ``Factory``
(never hardcoded), so the CLI (Phase 6) is a thin wrapper: parse
args → build an :class:`Engine` from the composition root → call ``run``/one of
the stage methods → format the returned ``Result``. No ``typer`` import anywhere
here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rusty_results.prelude import Err, Ok, Result

from jobfucker.ai import AiClient
from jobfucker.clients.base import Client, SearchWindow
from jobfucker.clients.factory import Factory
from jobfucker.config import PipelineConfig, search_index_range_error
from jobfucker.hh_tests.ai import HhTestAiSolver
from jobfucker.hh_tests.contract import HhTestCapable, HhTestSolver
from jobfucker.hh_tests.dump import HhTestDumpRecord
from jobfucker.hh_tests.selector import select_hh_test_solver
from jobfucker.reporting import NullReporter, Reporter
from jobfucker.stages.apply import ApplyFilters, ApplyReport, ApplyTargets, run_apply, select_candidates
from jobfucker.stages.fetch import FetchInputs, FetchReport, run_fetch
from jobfucker.stages.generate_cv import GenerateCvReport, run_generate_cv
from jobfucker.stages.prompts import PromptInputs, PromptTemplate, parse_prompt_template
from jobfucker.stages.score import ScoreReport, run_score
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline


@dataclass(frozen=True, slots=True)
class PipelineRunReport:
    """One report per stage for a full ``run`` (avoids a raw dict at the API)."""

    fetch: tuple[FetchReport, ...]  # one report per executed search-pool entry, in pool order
    score: ScoreReport
    generate_cv: GenerateCvReport
    apply: ApplyReport


# The position stages share the batch selector but each has its own
# stage-relative "already processed" predicate (see ``_positions``).
type ProcessingStage = Literal["score", "generate_cv", "apply"]


@dataclass(frozen=True, slots=True)
class BatchSelector:
    """How a stage/pipeline selects vacancies to act on (offset semantics).

    ``from_`` (inclusive, 0-based), ``to`` (inclusive) and ``take`` (count) are
    mutually-related views of the same window over the **id-ordered vacancy
    list** at run time (there is no stored position column).
    ``skip_already_processed`` excludes vacancies that already carry the
    running stage's own result (stage-relative; see :meth:`Engine._positions`).
    """

    from_: int | None = None
    to: int | None = None
    take: int | None = None
    skip_already_processed: bool = False

    def validate(self) -> Result[None, str]:
        """Fail fast on contradictory/negative batch arguments."""
        if self.to is not None and self.take is not None:
            return Err("Cannot combine --to with --take")
        if self.from_ is not None and self.from_ < 0:
            return Err("--from must be >= 0")
        if self.to is not None and self.from_ is not None and self.to < self.from_:
            return Err("--to must be >= --from")
        if self.take is not None and self.take <= 0:
            return Err("--take must be >= 1")
        return Ok(None)

    def positions(self, total: int) -> set[int]:
        """The 0-based position indices this selector targets, bounded by ``total``.

        ``take`` without ``from`` starts at position 0; ``to`` is inclusive.
        """
        start = self.from_ if self.from_ is not None else 0
        start = max(start, 0)
        if self.take is not None:
            end = start + self.take
        elif self.to is not None:
            end = self.to + 1
        else:
            end = total
        end = min(end, total)
        return set(range(start, end))


class Engine:
    """Composition root for one pipeline run: resolves deps, runs stages.

    Args:
        storage: the storage facade (four repositories).
        factory: the client registry (resolves the client bound to the run's
            config section).
        config: the validated pipeline config (query, section, resume, prompts).
        ai: the typed AI client (scoring + cover-letter generation).
        snapshot_id: the id of the :class:`PipelineSnapshot` whose config produced
            this run — every row the stages write is stamped with it (via the
            ``*_snapshot_id`` provenance columns) and audit entries record it.
        progress: the live output sink (:data:`NullReporter` default) forwarded
            to every stage and (through the client deps) the run's client.
    """

    def __init__(  # noqa: PLR0913 - composition root: all deps are DI seams (storage/factory/config/ai/snapshot/progress)
        self,
        *,
        storage: Storage,
        factory: Factory,
        config: PipelineConfig,
        ai: AiClient,
        snapshot_id: int,
        progress: Reporter | None = None,
    ) -> None:
        self._storage: Storage = storage
        self._factory: Factory = factory
        self._config: PipelineConfig = config
        self._ai: AiClient = ai
        self._snapshot_id: int = snapshot_id
        self._progress: Reporter = progress if progress is not None else NullReporter()

    # --- public API ----------------------------------------------------------
    async def run(
        self,
        pipeline: Pipeline,
        *,
        selector: BatchSelector | None = None,
        fetch_params: SearchWindow | None = None,
    ) -> Result[PipelineRunReport, str]:
        """Run fetch → score → generate_cv → apply in sequence.

        Args:
            pipeline: the stored pipeline DTO (must have an id).
            selector: the batch selector controlling the three position stages
                (fetch walks the pipeline's search pool — see ``fetch_params``).
            fetch_params: a run-wide fetch window/slice override; ``None`` =
                every pool entry uses its own configured window (or the
                defaults when it configures none).

        Returns:
            ``Ok`` with one concrete report per stage, or the first whole-batch
            ``Err``. Per-item failures are inside the reports (fields), not this
            ``Err``.
        """
        resolved = self._resolve_selector(selector)
        if resolved.is_err:
            return Err(resolved.unwrap_err())
        effective = resolved.unwrap()

        fetch_result = await self.fetch(pipeline, params=fetch_params)
        if fetch_result.is_err:
            return Err(fetch_result.unwrap_err())
        score_positions = await self._positions(pipeline, effective, stage="score")
        score_result = await self._run_score(pipeline, score_positions)
        if score_result.is_err:
            return Err(score_result.unwrap_err())
        cv_positions = await self._positions(pipeline, effective, stage="generate_cv")
        cv_result = await self._run_generate_cv(pipeline, cv_positions)
        if cv_result.is_err:
            return Err(cv_result.unwrap_err())
        apply_result = await self._run_apply(pipeline, effective)
        if apply_result.is_err:
            return Err(apply_result.unwrap_err())

        return Ok(
            PipelineRunReport(
                fetch=fetch_result.unwrap(),
                score=score_result.unwrap(),
                generate_cv=cv_result.unwrap(),
                apply=apply_result.unwrap(),
            )
        )

    async def fetch(
        self,
        pipeline: Pipeline,
        *,
        params: SearchWindow | None = None,
        only_search: int | None = None,
        refresh: bool = False,
    ) -> Result[tuple[FetchReport, ...], str]:
        """Fetch the pipeline's search pool into the DB, entry by entry.

        The pipeline's ordered ``searches`` pool is walked in order; each entry
        is an insert-only mirror sync (existing rows are never touched, so
        prior processed state survives), and already-stored ids — including
        ones an earlier entry just persisted — are excluded client-side, so
        pool overlap costs a skip, not a re-fetch. ``refresh=True`` opts into
        overwriting **listing fields only**; soft-deleted rows are skipped even
        under refresh.

        Args:
            pipeline: the stored pipeline DTO.
            params: a run-wide window/slice override applied to **every**
                executed entry; ``None`` = each entry uses its own configured
                window (or the defaults when it configures none).
            only_search: restrict the walk to one pool entry (its configured
                window, unless ``params`` overrides it); out of range = ``Err``.
            refresh: the ``--refresh`` listing-fields-only opt-in.

        Returns:
            ``Ok`` with one report per executed entry (in pool order), or the
            ``Err`` of the first entry that failed (a mid-walk page failure has
            already persisted that entry's loaded items; later entries are not
            attempted).
        """
        return await self._run_fetch(pipeline, params=params, only_search=only_search, refresh=refresh)

    async def score(
        self,
        pipeline: Pipeline,
        *,
        selector: BatchSelector | None = None,
    ) -> Result[ScoreReport, str]:
        """Score the selector window of vacancies for the pipeline."""
        positions_result = await self._resolve_positions(pipeline, selector, stage="score")
        if positions_result.is_err:
            return Err(positions_result.unwrap_err())
        return await self._run_score(pipeline, positions_result.unwrap())

    async def generate_cv(
        self,
        pipeline: Pipeline,
        *,
        selector: BatchSelector | None = None,
    ) -> Result[GenerateCvReport, str]:
        """Generate cover letters for the selector window of vacancies."""
        positions_result = await self._resolve_positions(pipeline, selector, stage="generate_cv")
        if positions_result.is_err:
            return Err(positions_result.unwrap_err())
        return await self._run_generate_cv(pipeline, positions_result.unwrap())

    async def apply(
        self,
        pipeline: Pipeline,
        *,
        selector: BatchSelector | None = None,
        filters: ApplyFilters | None = None,
        test_answers: Path | None = None,
        no_test_ai: bool = False,
    ) -> Result[ApplyReport, str]:
        """Apply to the selector window of vacancies (with dual-limit checks).

        ``filters`` carries the per-run eligibility overrides
        (:class:`~jobfucker.stages.apply.ApplyFilters`); ``None`` = the strict
        automatic filters. When ``filters.only_external_ids`` is set it
        replaces the selector window (the CLI rejects combining them), and
        every requested id must exist in the pipeline — otherwise the whole
        run fails fast with an ``Err`` naming the missing ids (a silent
        empty selection would read as a successful no-op).

        ``test_answers``/``no_test_ai`` drive the HH screening-test solver
        (``--test-answers FILE`` / ``--no-test-ai``): the answers file wins,
        then the configured AI path (config ``hh_test_solving`` section),
        mirroring captcha handler selection.
        """
        resolved = self._resolve_selector(selector)
        if resolved.is_err:
            return Err(resolved.unwrap_err())
        effective = filters if filters is not None else ApplyFilters()
        if effective.only_external_ids:
            known = {v.external_id for v in await self._storage.vacancies.list_by_pipeline(pipeline.id)}
            missing = sorted(effective.only_external_ids - known)
            if missing:
                return Err(f"Vacancy id(s) not found in pipeline {pipeline.id}: {', '.join(missing)}")
        return await self._run_apply(
            pipeline,
            resolved.unwrap(),
            effective,
            test_answers=test_answers,
            no_test_ai=no_test_ai,
        )

    async def dump_hh_tests(
        self,
        pipeline: Pipeline,
        *,
        selector: BatchSelector | None = None,
        filters: ApplyFilters | None = None,
    ) -> Result[tuple[HhTestDumpRecord, ...], str]:
        """Fetch the screening tests of eligible test-bearing vacancies.

        The ``hh-tests dump`` backend: selects candidates exactly like apply
        (same eligibility rules + ``has_hh_test`` filter), probes only
        test-bearing rows via the client's ``get_vacancy_test``, and returns
        the fetched problems (no solving — that is the separate solve step).
        A board that is not :class:`HhTestCapable` is a clear ``Err``; a
        per-vacancy fetch failure aborts the dump (read-only probe, same
        fail-fast semantics as fetch).
        """
        resolved = self._resolve_selector(selector)
        if resolved.is_err:
            return Err(resolved.unwrap_err())
        effective_filters = filters if filters is not None else ApplyFilters()
        positions = (
            None
            if effective_filters.only_external_ids
            else await self._positions(pipeline, resolved.unwrap(), stage="apply")
        )
        candidates, _ = await select_candidates(
            self._storage,
            pipeline,
            self._config.scoring.min_required_score,
            positions=positions,
            filters=effective_filters,
        )
        client = self._client()
        try:
            if not isinstance(client, HhTestCapable):
                return Err(f"Board {self._config.service!r} does not support hh screening tests (hh-tests dump)")
            capable: HhTestCapable = client
            records: list[HhTestDumpRecord] = []
            for vacancy in candidates:
                if vacancy.has_hh_test is not True:
                    continue
                problem_result = await capable.get_vacancy_test(vacancy.external_id)
                if problem_result.is_err:
                    message = problem_result.unwrap_err().message
                    return Err(f"hh-tests dump: vacancy {vacancy.external_id} test fetch failed: {message}")
                problem = problem_result.unwrap()
                if problem is not None:
                    records.append(HhTestDumpRecord(problem=problem, title=vacancy.title, url=vacancy.url))
            return Ok(tuple(records))
        finally:
            await client.aclose()

    # --- per-stage dispatch --------------------------------------------------
    async def _run_fetch(
        self,
        pipeline: Pipeline,
        *,
        params: SearchWindow | None = None,
        only_search: int | None = None,
        refresh: bool = False,
    ) -> Result[tuple[FetchReport, ...], str]:
        entries = self._config.service_section.searches
        if only_search is not None and not 0 <= only_search < len(entries):
            return Err(search_index_range_error(len(entries)))
        indices = range(len(entries)) if only_search is None else (only_search,)
        client = self._client()
        try:
            reports: list[FetchReport] = []
            for index in indices:
                entry = entries[index]
                # Run-wide override wins wholesale; otherwise the entry's own
                # window, or the SearchWindow defaults when it configures none.
                effective = (
                    params if params is not None else (entry.window if entry.window is not None else SearchWindow())
                )
                result = await run_fetch(
                    self._storage,
                    pipeline,
                    FetchInputs(
                        client=client,
                        search_index=index,
                        query=entry.query,
                        params=effective,
                        refresh=refresh,
                    ),
                    snapshot_id=self._snapshot_id,
                    progress=self._progress,
                    error_origin=f"search {index}: ",
                )
                if result.is_err:
                    # Stop the pool walk at the first failing entry: its loaded
                    # items are persisted, later entries stay pending.
                    return Err(result.unwrap_err())
                reports.append(result.unwrap())
            return Ok(tuple(reports))
        finally:
            await client.aclose()

    async def _run_apply(
        self,
        pipeline: Pipeline,
        selector: BatchSelector,
        filters: ApplyFilters | None = None,
        *,
        test_answers: Path | None = None,
        no_test_ai: bool = False,
    ) -> Result[ApplyReport, str]:
        # Forced-ids mode selects by external id stage-side; no offset window.
        effective_filters = filters if filters is not None else ApplyFilters()
        positions = (
            None if effective_filters.only_external_ids else await self._positions(pipeline, selector, stage="apply")
        )
        wiring = self._resolve_hh_test_solver_wiring(answers_file=test_answers, no_test_ai=no_test_ai)
        if wiring.is_err:
            return Err(wiring.unwrap_err())
        solver, prompt_inputs = wiring.unwrap()
        client = self._client()
        try:
            return await run_apply(
                self._storage,
                pipeline,
                ApplyTargets(client=client, resume_id=self._config.service_section.resume_id),
                positions=positions,
                snapshot_id=self._snapshot_id,
                min_required_score=self._config.scoring.min_required_score,
                daily_apply_limit=self._config.limits.daily_apply_limit,
                login=self._config.auth.login,
                service=self._config.service,
                filters=effective_filters,
                progress=self._progress,
                hh_test_solver=solver,
                hh_test_prompt=prompt_inputs,
            )
        finally:
            await client.aclose()

    def _resolve_hh_test_solver_wiring(
        self,
        *,
        answers_file: Path | None,
        no_test_ai: bool,
    ) -> Result[tuple[HhTestSolver | None, PromptInputs | None], str]:
        """Resolve the HH test solver + its prompt inputs from the run flags.

        The solver chain mirrors captcha selection: the explicit answers file
        wins, then the configured AI path (``hh_test_solving`` section + main
        ``openai``), unless ``--no-test-ai``. The prompt inputs (parsed template
        + resume) are only built for the AI solver; the selector has already
        vetted the template, so a file-only run never parses it and a broken
        template cannot abort a run that supplied ``--test-answers``.
        """
        solver = select_hh_test_solver(
            self._config,
            answers_file=answers_file,
            no_test_ai=no_test_ai,
        )
        prompt_inputs: PromptInputs | None = None
        section = self._config.hh_test_solving
        if isinstance(solver, HhTestAiSolver) and section is not None:
            parsed = parse_prompt_template(section.test_prompt)
            if parsed.is_ok:
                prompt_inputs = PromptInputs(prompt=parsed.unwrap(), resume=self._config.resume.contents)
        return Ok((solver, prompt_inputs))

    async def _run_score(self, pipeline: Pipeline, positions: set[int]) -> Result[ScoreReport, str]:
        inputs = self._inputs(
            self._config.scoring.scoring_prompt,
            self._config.resume.contents,
        )
        if inputs.is_err:
            return Err(inputs.unwrap_err())
        return await run_score(
            self._storage,
            pipeline,
            self._ai,
            inputs.unwrap(),
            positions=positions,
            snapshot_id=self._snapshot_id,
            min_required_score=self._config.scoring.min_required_score,
            progress=self._progress,
        )

    async def _run_generate_cv(self, pipeline: Pipeline, positions: set[int]) -> Result[GenerateCvReport, str]:
        inputs = self._inputs(
            self._config.apply.apply_prompt,
            self._config.resume.contents,
        )
        if inputs.is_err:
            return Err(inputs.unwrap_err())
        return await run_generate_cv(
            self._storage,
            pipeline,
            self._ai,
            inputs.unwrap(),
            positions=positions,
            snapshot_id=self._snapshot_id,
            min_required_score=self._config.scoring.min_required_score,
            progress=self._progress,
        )

    # --- shared resolution helpers ------------------------------------------
    def _client(self) -> Client:
        # The factory already holds the run's bound config section, so no
        # section is threaded through the engine.
        return self._factory.get(self._config.service)

    def _resolve_selector(self, selector: BatchSelector | None) -> Result[BatchSelector, str]:
        """Default a missing selector to "everything" and validate it."""
        effective = selector if selector is not None else BatchSelector()
        validated = effective.validate()
        if validated.is_err:
            return Err(validated.unwrap_err())
        return Ok(effective)

    async def _resolve_positions(
        self,
        pipeline: Pipeline,
        selector: BatchSelector | None,
        *,
        stage: ProcessingStage,
    ) -> Result[set[int], str]:
        """Validate the selector and map it onto concrete vacancy positions."""
        resolved = self._resolve_selector(selector)
        if resolved.is_err:
            return Err(resolved.unwrap_err())
        return Ok(await self._positions(pipeline, resolved.unwrap(), stage=stage))

    def _inputs(self, prompt_text: str, resume: str) -> Result[PromptInputs, str]:
        template_result = parse_prompt_template(prompt_text)
        if template_result.is_err:
            return Err(template_result.unwrap_err())
        template: PromptTemplate = template_result.unwrap()
        return Ok(PromptInputs(resume=resume, prompt=template))

    async def _positions(self, pipeline: Pipeline, selector: BatchSelector, *, stage: ProcessingStage) -> set[int]:
        """The list offsets to act on, after applying ``skip_already_processed``.

        ``from_/to/take`` select a window of offsets into the id-ordered
        ``list_by_pipeline`` list; when skipping already-processed, the offsets
        of vacancies that already carry the **running stage's** result are
        removed from the window (stage-relative: a score is a result only for
        ``score``, a cover letter only for ``generate``, an apply decision
        only for ``apply``).
        """
        all_vacancies = await self._storage.vacancies.list_by_pipeline(pipeline.id)
        positions = selector.positions(len(all_vacancies))
        if selector.skip_already_processed:
            repository = self._storage.vacancies
            match stage:
                case "score":
                    already = await repository.external_ids_scored(pipeline.id)
                case "generate_cv":
                    already = await repository.external_ids_with_cover_letter(pipeline.id)
                case "apply":
                    already = await repository.external_ids_decided(pipeline.id)
            if already:
                excluded = {i for i, v in enumerate(all_vacancies) if v.external_id in already}
                positions = positions - excluded
        return positions


__all__ = ["BatchSelector", "Engine", "PipelineRunReport", "ProcessingStage"]
