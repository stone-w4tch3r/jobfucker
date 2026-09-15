"""Phase 5A (task 5.4): Gherkin acceptance for the cover-letter stage.

Behavioral scenarios (generate for eligible; skip manual-skip / sub-threshold;
per-item ``cover_letter_error``) are expressed as a ``.feature`` with typed
``pytest-bdd`` steps. The AI is a scripted stub implementing :class:`AiClient`.

Async-migration BDD convention (D1): steps are **synchronous** and call the
async domain through :func:`jobfucker.testing.step_runner.async_run` /
:func:`~jobfucker.testing.step_runner.async_run_result`.
"""

from __future__ import annotations

from dataclasses import dataclass

from pytest_bdd import given, parsers, scenarios, then, when
from rusty_results.prelude import Err, Ok, Result

from jobfucker.ai import ScoreError, ScoreResult
from jobfucker.clients.base import ServiceVacancyId
from jobfucker.stages.generate_cv import GenerateCvReport, run_generate_cv
from jobfucker.stages.prompts import PromptInputs, PromptTemplate
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, VacancyRecord
from jobfucker.storage.models import AuditLog
from jobfucker.testing.step_runner import async_run, async_run_result
from test.pipeline_helpers import create_pipeline, snapshot_for
from test.storage.builders import build_vacancy

scenarios("bdd/generate_cv.feature")


class ScriptedCvAi:
    """An :class:`AiClient` stub returning scripted cover letters in call order."""

    def __init__(self, letters: list[Result[str, str]]) -> None:
        self._letters = list(letters)

    async def score(self, prompt: str) -> Result[ScoreResult, ScoreError]:
        del prompt
        raise AssertionError("cover-letter stage must not score")

    async def generate_cover_letter(self, prompt: str) -> Result[str, str]:
        del prompt
        return self._letters.pop(0)


@dataclass(frozen=True, slots=True)
class GenerateOutcome:
    """The report plus the persisted vacancies, for cross-step assertion."""

    report: GenerateCvReport
    vacancies: list[VacancyRecord]


def _apply_inputs() -> PromptInputs:
    template = PromptTemplate(params={}, body="Letter for {{ vacancy_formatted }}: {{ resume_formatted }}")
    return PromptInputs(resume="resume contents", prompt=template)


def _by_external_id(outcome: GenerateOutcome, external_id: str) -> VacancyRecord:
    for vacancy in outcome.vacancies:
        if vacancy.external_id == ServiceVacancyId(external_id):
            return vacancy
    raise AssertionError(f"no vacancy with external_id {external_id!r}")


@given("a pipeline with min_required_score 3", target_fixture="pipeline")
def pipeline_step(storage: Storage) -> Pipeline:
    """Create a pipeline identity + head snapshot with the pass threshold at 3."""
    return async_run(create_pipeline(storage, name="generate-bdd", min_required_score=3))


@given('an eligible scored vacancy "good" with score 5 and a sub-threshold "weak" with score 2')
def seed_scored_good_and_weak(storage: Storage, pipeline: Pipeline) -> None:
    """Seed an above-threshold and a below-threshold scored vacancy."""
    async_run(
        storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id="good",
                title="Senior Backend",
                score=5,
                score_reasoning="strong",
            )
        )
    )
    async_run(
        storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id="weak",
                title="Junior Intern",
                score=2,
                score_reasoning="weak",
            )
        )
    )


@given('a manually-skipped vacancy "blocked" with score 5 and an eligible vacancy "ok" with score 4')
def seed_manual_skip_and_ok(storage: Storage, pipeline: Pipeline) -> None:
    """Seed a manual-skip (ineligible) and an eligible scored vacancy."""
    async_run(
        storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id="blocked",
                title="Blocked",
                score=5,
                manual_skip=True,
            )
        )
    )
    async_run(
        storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id="ok",
                title="OK Role",
                score=4,
                score_reasoning="fine",
            )
        )
    )


@given('an eligible scored vacancy "good" with score 5 and another eligible "also" with score 5')
def seed_two_eligible(storage: Storage, pipeline: Pipeline) -> None:
    """Seed two eligible, above-threshold scored vacancies."""
    async_run(
        storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id="good",
                title="Good Role",
                score=5,
                score_reasoning="a",
            )
        )
    )
    async_run(
        storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id="also",
                title="Also Role",
                score=5,
                score_reasoning="b",
            )
        )
    )


@given(parsers.parse('an AI that generates the letter "{letter}"'), target_fixture="ai")
def ai_generates_letter(letter: str) -> ScriptedCvAi:
    """Script the stub to return a single letter (only one eligible vacancy)."""
    return ScriptedCvAi([Ok(letter)])


@given('an AI that fails the first letter then generates "done"', target_fixture="ai")
def ai_fails_then_done() -> ScriptedCvAi:
    """Script the stub: first eligible fails, second succeeds with 'done'."""
    return ScriptedCvAi([Err("AI cover-letter call failed: boom"), Ok("done")])


@when("the cover-letter stage runs over the pipeline", target_fixture="generate_outcome")
def run_generate_stage(storage: Storage, pipeline: Pipeline, ai: ScriptedCvAi) -> GenerateOutcome:
    """Run the cover-letter stage and capture its report + persisted vacancies."""
    snapshot = async_run(snapshot_for(storage, pipeline))
    report = async_run_result(
        run_generate_cv(
            storage,
            pipeline,
            ai,
            _apply_inputs(),
            snapshot_id=snapshot.id,
            min_required_score=snapshot.min_required_score,
        )
    )
    return GenerateOutcome(
        report=report,
        vacancies=async_run(storage.vacancies.list_by_pipeline(pipeline.id)),
    )


@then(parsers.parse('"{external_id}" has the generated cover letter'))
def assert_generated(generate_outcome: GenerateOutcome, external_id: str) -> None:
    """The eligible vacancy carries the generated cover letter."""
    vacancy = _by_external_id(generate_outcome, external_id)
    assert vacancy.cover_letter is not None
    assert vacancy.cover_letter_error is None


@then(parsers.parse('"{external_id}" is skipped and has no cover letter'))
def assert_skipped_no_letter(generate_outcome: GenerateOutcome, external_id: str) -> None:
    """The ineligible vacancy is left untouched (no cover letter)."""
    vacancy = _by_external_id(generate_outcome, external_id)
    assert vacancy.cover_letter is None
    assert vacancy.cover_letter_error is None


@then("the generate report shows 1 generated and 1 skipped")
def assert_1_generated_1_skipped(generate_outcome: GenerateOutcome) -> None:
    """The report aggregate reflects one generated + one skipped (no failures)."""
    report = generate_outcome.report
    assert report.generated == 1
    assert report.skipped == 1
    assert report.failed == 0
    assert report.total == 2


@then(parsers.parse('"{external_id}" has the generated cover letter "{letter}"'))
def assert_generated_with_text(generate_outcome: GenerateOutcome, external_id: str, letter: str) -> None:
    """The eligible vacancy carries exactly the generated text."""
    vacancy = _by_external_id(generate_outcome, external_id)
    assert vacancy.cover_letter == letter
    assert vacancy.cover_letter_error is None


@then(parsers.parse('"{external_id}" has a cover_letter_error'))
def assert_error(generate_outcome: GenerateOutcome, external_id: str) -> None:
    """The eligible-but-failed vacancy stores a per-item cover_letter_error."""
    vacancy = _by_external_id(generate_outcome, external_id)
    assert vacancy.cover_letter is None
    assert vacancy.cover_letter_error is not None


@then("the generate report shows 1 generated and 1 failed")
def assert_1_generated_1_failed(generate_outcome: GenerateOutcome) -> None:
    """The report aggregate reflects one generated + one per-item failure."""
    report = generate_outcome.report
    assert report.generated == 1
    assert report.failed == 1
    assert report.skipped == 0
    assert report.total == 2


# --- plain-pytest edge coverage ------------------------------------------------
async def test_generate_skips_terminal_apply_states(storage: Storage) -> None:
    """A vacancy already in a terminal apply state is excluded.

    ``apply_status`` is the apply stage's vocabulary: ``applied`` (accepted),
    ``skipped`` (declined) and ``error`` (failed attempt) are all terminal —
    none warrant a fresh cover letter. A sub-threshold row (score below the
    threshold, no apply status) stays a candidate in the sense of not being
    terminal, but is still ineligible on score below minimum.
    """
    pipeline = await create_pipeline(storage, name="terminal", min_required_score=3)
    snapshot = await snapshot_for(storage, pipeline)
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="done",
            score=5,
            score_reasoning="ok",
            apply_status="applied",
        )
    )
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="declined",
            score=5,
            score_reasoning="ok",
            apply_status="skipped",
        )
    )
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="broken",
            score=5,
            score_reasoning="ok",
            apply_status="error",
        )
    )
    # No eligible vacancy -> generate_cover_letter must never be called.
    ai = ScriptedCvAi([])

    report = await run_generate_cv(
        storage, pipeline, ai, _apply_inputs(), snapshot_id=snapshot.id, min_required_score=snapshot.min_required_score
    )
    assert report.is_ok
    rep = report.unwrap()
    assert rep.generated == 0
    assert rep.skipped == 3
    stored = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    assert all(v.cover_letter is None for v in stored.values())


async def test_generate_is_idempotent_for_existing_cover_letter(storage: Storage) -> None:
    """A vacancy that already has a cover letter is not regenerated."""
    pipeline = await create_pipeline(storage, name="idem", min_required_score=3)
    snapshot = await snapshot_for(storage, pipeline)
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="has-letter",
            score=5,
            score_reasoning="ok",
            cover_letter="existing",
        )
    )
    ai = ScriptedCvAi([])  # must not be called

    report = await run_generate_cv(
        storage, pipeline, ai, _apply_inputs(), snapshot_id=snapshot.id, min_required_score=snapshot.min_required_score
    )
    assert report.is_ok
    rep = report.unwrap()
    assert rep.generated == 0
    assert rep.skipped == 1
    vacancy = (await storage.vacancies.list_by_pipeline(pipeline.id))[0]
    assert vacancy.cover_letter == "existing"


async def test_generate_writes_an_audit_entry(storage: Storage) -> None:
    """The cover-letter stage appends one 'generate' audit_log entry per run."""
    pipeline = await create_pipeline(storage, name="audit", min_required_score=3)
    snapshot = await snapshot_for(storage, pipeline)
    await storage.vacancies.upsert(
        build_vacancy(pipeline_id=pipeline.id, external_id="ok", score=5, score_reasoning="ok")
    )
    ai = ScriptedCvAi([Ok("letter")])

    report = await run_generate_cv(
        storage, pipeline, ai, _apply_inputs(), snapshot_id=snapshot.id, min_required_score=snapshot.min_required_score
    )
    assert report.is_ok

    entries = await _audit_entries(storage, pipeline.id)
    assert [entry.action for entry in entries] == ["generate"]
    assert entries[0].details is not None
    assert '"generated": 1' in entries[0].details
    assert entries[0].pipeline_snapshot_id == snapshot.id

    # The cover-letter row is stamped with the snapshot whose config generated it.
    generated = (await storage.vacancies.list_by_pipeline(pipeline.id))[0]
    assert generated.generated_snapshot_id == snapshot.id


async def _audit_entries(storage: Storage, pipeline_id: int) -> list[AuditLog]:
    """All audit entries for a pipeline, via a fresh session (append is committed)."""
    from sqlalchemy import select

    from jobfucker.storage.models import AuditLog

    async with storage.session_factory() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.pipeline_id == pipeline_id))).scalars().all()
        return list(rows)
