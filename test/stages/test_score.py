"""Phase 5A (task 5.3): Gherkin acceptance for the scoring stage.

Behavioral scenarios (sub-threshold score → stored score, ``apply_status``
untouched; out-of-range AI score → per-vacancy ``score_error`` without aborting
the batch) are expressed as a ``.feature`` with typed ``pytest-bdd`` steps. The
AI is a scripted stub (:class:`ScriptedAi`) implementing the :class:`AiClient`
protocol, so the stage is exercised through the real :class:`Storage` without
any network.

Async-migration BDD convention (D1): steps are **synchronous** and call the
async domain through :func:`jobfucker.testing.step_runner.async_run` /
:func:`~jobfucker.testing.step_runner.async_run_result`.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from pytest_bdd import given, scenarios, then, when
from rusty_results.prelude import Err, Ok, Result

from jobfucker.ai import ScoreError, ScoreResult
from jobfucker.clients.base import ServiceVacancyId
from jobfucker.stages.prompts import PromptInputs
from jobfucker.stages.score import ScoreReport, run_score
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, VacancyRecord
from jobfucker.storage.models import AuditLog
from jobfucker.testing.step_runner import async_run, async_run_result
from test.pipeline_helpers import create_pipeline, snapshot_for
from test.storage.builders import build_vacancy, make_snapshot

scenarios("bdd/score.feature")


class ScriptedAi:
    """An :class:`AiClient` stub returning scripted results in call order."""

    def __init__(self, scores: list[Result[ScoreResult, ScoreError]]) -> None:
        self._scores = list(scores)

    async def score(self, prompt: str) -> Result[ScoreResult, ScoreError]:
        del prompt
        return self._scores.pop(0)

    async def generate_cover_letter(self, prompt: str) -> Result[str, str]:
        del prompt
        raise AssertionError("score stage must not generate cover letters")


@dataclass(frozen=True, slots=True)
class ScoreOutcome:
    """The report plus the persisted vacancies, for cross-step assertion."""

    report: ScoreReport
    vacancies: list[VacancyRecord]


def _score_inputs() -> PromptInputs:
    return PromptInputs(
        resume="resume contents",
        prompt="Score {{ resume_formatted }} for {{ vacancy_formatted }}",
    )


@given("a pipeline with min_required_score 3", target_fixture="pipeline")
def pipeline_step(storage: Storage) -> Pipeline:
    """Create a pipeline identity + head snapshot with the pass threshold at 3."""
    return async_run(create_pipeline(storage, name="score-bdd", min_required_score=3))


@given("two vacancies seeded at position 0 and 1")
def seed_two_vacancies(storage: Storage, pipeline: Pipeline) -> None:
    """Seed two vacancies (position 0 and 1) under the pipeline."""
    async_run(
        storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="good", title="Senior Backend"))
    )
    async_run(
        storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="weak", title="Junior Intern"))
    )


@given("an AI that scores them 5 then 2", target_fixture="ai")
def ai_scores_5_then_2() -> ScriptedAi:
    """Script the stub: position 0 -> 5, position 1 -> 2."""
    return ScriptedAi(
        [
            Ok(ScoreResult(fit_score=5, comment="strong match")),
            Ok(ScoreResult(fit_score=2, comment="weak match")),
        ]
    )


@given("an AI that returns an out-of-range score then a valid score 4", target_fixture="ai")
def ai_out_of_range_then_4() -> ScriptedAi:
    """Script the stub: position 0 -> out-of-range error, position 1 -> 4."""
    return ScriptedAi(
        [
            Err(ScoreError(message="AI fit_score 9 is out of range 1..5", raw='{"fit_score": 9}')),
            Ok(ScoreResult(fit_score=4, comment="fine")),
        ]
    )


@when("the score stage runs over the pipeline", target_fixture="score_outcome")
def run_score_stage(storage: Storage, pipeline: Pipeline, ai: ScriptedAi) -> ScoreOutcome:
    """Run the score stage and capture its report + persisted vacancies."""
    snapshot = async_run(snapshot_for(storage, pipeline))
    report = async_run_result(
        run_score(
            storage,
            pipeline,
            ai,
            _score_inputs(),
            snapshot_id=snapshot.id,
            min_required_score=snapshot.min_required_score,
        )
    )
    return ScoreOutcome(
        report=report,
        vacancies=async_run(storage.vacancies.list_by_pipeline(pipeline.id)),
    )


@then("the position 0 vacancy has score 5 and is not skipped")
def assert_position0_scored(score_outcome: ScoreOutcome) -> None:
    """Position 0 keeps its score; the score stage never stamps apply_status."""
    vacancy = score_outcome.vacancies[0]
    assert vacancy.score == 5
    assert vacancy.score_reasoning == "strong match"
    assert vacancy.score_error is None
    assert vacancy.apply_status is None


@then("the position 1 vacancy has score 2 and no apply status")
def assert_position1_subthreshold(score_outcome: ScoreOutcome) -> None:
    """Position 1 is sub-threshold: the score is stored, apply_status untouched.

    Sub-threshold is a derived read (``score`` vs the snapshot threshold),
    not a stored state — ``apply_status`` belongs to the apply stage.
    """
    vacancy = score_outcome.vacancies[1]
    assert vacancy.score == 2
    assert vacancy.score_reasoning == "weak match"
    assert vacancy.apply_status is None


@then("the score report shows 2 scored and 1 sub-threshold")
def assert_report_counts(score_outcome: ScoreOutcome) -> None:
    """The report aggregate reflects 2 scored and 1 sub-threshold skip."""
    report = score_outcome.report
    assert report.scored == 2
    assert report.sub_threshold == 1
    assert report.failed == 0
    assert report.total == 2


@then("the position 0 vacancy has a score_error and no score")
def assert_position0_error(score_outcome: ScoreOutcome) -> None:
    """Position 0's out-of-range score is stored as a score_error, not a crash."""
    vacancy = score_outcome.vacancies[0]
    assert vacancy.score is None
    assert vacancy.score_error is not None
    assert "out of range" in vacancy.score_error


@then("the position 1 vacancy is scored 4")
def assert_position1_scored(score_outcome: ScoreOutcome) -> None:
    """The batch continued and position 1 was scored despite position 0 failing."""
    vacancy = score_outcome.vacancies[1]
    assert vacancy.score == 4
    assert vacancy.score_error is None


@then("the score report shows 1 scored and 1 failed")
def assert_report_mixed_counts(score_outcome: ScoreOutcome) -> None:
    """The aggregate reflects one success and one per-item failure."""
    report = score_outcome.report
    assert report.scored == 1
    assert report.failed == 1
    assert report.sub_threshold == 0
    assert report.total == 2


# --- plain-pytest edge coverage ------------------------------------------------
async def test_score_writes_an_audit_entry(storage: Storage) -> None:
    """The score stage appends one 'score' audit_log entry per run."""
    pipeline = await create_pipeline(storage, name="audit", min_required_score=3)
    snapshot = await snapshot_for(storage, pipeline)
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="v"))
    ai = ScriptedAi([Ok(ScoreResult(fit_score=4, comment="ok"))])

    report = await run_score(
        storage,
        pipeline,
        ai,
        _score_inputs(),
        snapshot_id=snapshot.id,
        min_required_score=snapshot.min_required_score,
    )
    assert report.is_ok

    entries = await _audit_entries(storage, pipeline.id)
    assert [entry.action for entry in entries] == ["score"]
    assert entries[0].details is not None
    assert '"scored": 1' in entries[0].details
    assert entries[0].pipeline_snapshot_id == snapshot.id

    # The scored row is stamped with the snapshot whose config scored it.
    scored = await storage.vacancies.list_by_pipeline(pipeline.id)
    assert scored[0].scored_snapshot_id == snapshot.id


async def test_score_positions_filter_only_scores_selected_positions(storage: Storage) -> None:
    """Passing positions restricts which vacancies are scored."""
    pipeline = await create_pipeline(storage, name="pos", min_required_score=3)
    snapshot = await snapshot_for(storage, pipeline)
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="a"))
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="b"))
    # Only offset 1 is scored; offset 0 is ignored entirely.
    ai = ScriptedAi([Ok(ScoreResult(fit_score=4, comment="only b"))])

    report = await run_score(
        storage,
        pipeline,
        ai,
        _score_inputs(),
        snapshot_id=snapshot.id,
        min_required_score=snapshot.min_required_score,
        positions={1},
    )
    assert report.is_ok
    rep = report.unwrap()
    assert rep.total == 1
    assert rep.vacancies[0].external_id == _external_id("b")

    # Position 0 was never touched by the stage.
    untouched = (await storage.vacancies.list_by_pipeline(pipeline.id))[0]
    assert untouched.external_id == _external_id("a")
    assert untouched.score is None
    assert untouched.score_error is None


async def test_rescore_keeps_apply_outcome_untouched(storage: Storage) -> None:
    """A re-score under a new snapshot never touches the apply stage's fields.

    ``apply_status``/``applied_snapshot_id`` describe a board-side apply
    outcome. Re-scoring — any score, including sub-threshold — leaves them
    verbatim: sub-threshold is derived from ``score`` vs the snapshot's
    threshold, never a stored state.
    """
    pipeline = await create_pipeline(storage, name="rescore", min_required_score=3)
    snap_a = await snapshot_for(storage, pipeline)
    snap_b = await storage.snapshots.create(make_snapshot(pipeline_id=pipeline.id, snapshot_no=2))
    await storage.pipelines.set_current_snapshot(pipeline.id, snap_b.id)

    # Vacancy was applied under snapshot A.
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="v1",
            apply_status="applied",
            applied_snapshot_id=snap_a.id,
            applied_at="2026-08-05 10:00:00",
        )
    )
    ai = ScriptedAi([Ok(ScoreResult(fit_score=2, comment="weaker now"))])
    report = await run_score(
        storage,
        pipeline,
        ai,
        _score_inputs(),
        snapshot_id=snap_b.id,
        min_required_score=snap_b.min_required_score,
    )
    assert report.is_ok

    row = (await storage.vacancies.list_by_pipeline(pipeline.id))[0]
    assert row.score == 2  # sub-threshold score stored like any other
    assert row.apply_status == "applied"  # apply outcome survives verbatim
    assert row.applied_snapshot_id == snap_a.id  # and its provenance
    assert row.scored_snapshot_id == snap_b.id


async def test_scoring_writes_scored_at_and_clears_score_staleness(storage: Storage) -> None:
    """A score write bumps ``scored_at`` (a re-score after a refresh is not stale).

    ``score_stale = fetched_at > scored_at``; scoring stamps ``scored_at``
    with the current time, so a row refreshed before scoring reads as current
    (``scored_at >= fetched_at``), and a scoring error also bumps the stamp.
    """
    pipeline = await create_pipeline(storage, name="stale", min_required_score=3)
    snapshot = await snapshot_for(storage, pipeline)
    # Fetched at 12:00, scored at 09:00 -> stale before the stage runs.
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="v1",
            fetched_at="2026-08-05 12:00:00",
            scored_at="2026-08-05 09:00:00",
        )
    )
    ai = ScriptedAi([Ok(ScoreResult(fit_score=4, comment="ok"))])
    report = await run_score(
        storage,
        pipeline,
        ai,
        _score_inputs(),
        snapshot_id=snapshot.id,
        min_required_score=snapshot.min_required_score,
    )
    assert report.is_ok
    row = (await storage.vacancies.list_by_pipeline(pipeline.id))[0]
    assert row.fetched_at == "2026-08-05 12:00:00"
    assert row.scored_at is not None and row.scored_at >= row.fetched_at  # staleness cleared
    assert row.generated_at is None  # letter stamp untouched by scoring

    # A scoring failure also stamps scored_at (the artifact is current-until-write).
    await storage.vacancies.update(replace(row, fetched_at="2026-08-05 13:00:00", scored_at=None))
    failing = ScriptedAi([Err(ScoreError(message="boom", raw="{}"))])
    failed_report = await run_score(
        storage,
        pipeline,
        failing,
        _score_inputs(),
        snapshot_id=snapshot.id,
        min_required_score=snapshot.min_required_score,
    )
    assert failed_report.is_ok
    failed_row = (await storage.vacancies.list_by_pipeline(pipeline.id))[0]
    assert failed_row.score_error is not None
    assert failed_row.scored_at is not None
    assert failed_row.fetched_at is not None
    assert failed_row.scored_at >= failed_row.fetched_at


async def _audit_entries(storage: Storage, pipeline_id: int) -> list[AuditLog]:
    """All audit entries for a pipeline, via a fresh session (append is committed)."""
    from sqlalchemy import select

    from jobfucker.storage.db import SessionFactory
    from jobfucker.storage.models import AuditLog

    session_factory: SessionFactory = storage.session_factory
    async with session_factory() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.pipeline_id == pipeline_id))).scalars().all()
        return list(rows)


def _external_id(value: str) -> ServiceVacancyId:
    from jobfucker.clients.base import ServiceVacancyId

    return ServiceVacancyId(value)
