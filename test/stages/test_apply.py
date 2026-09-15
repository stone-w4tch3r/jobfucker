"""Phase 5B (task 5.5): apply stage coverage.

Exercises ``run_apply`` with a programmable mock client: outcome mapping to
``apply_status`` (applied/skipped/error — never ``rejected``), shared-limit
enforcement (both dual caps apply to the ONE per-auth counter),
:class:`LimitExceededError` stopping with an ``Ok`` ``limit_reached`` report,
idempotency (no double apply), provenance stamping (``applied_snapshot_id``),
and the SC4 rule: two pipelines on one account exhaust a single shared counter.
"""

from __future__ import annotations

from typing import Literal

import pytest
from rusty_results.prelude import Result
from sqlalchemy import select

from jobfucker.clients.base import Client, ClientDeps, ServiceVacancyId
from jobfucker.clients.factory import Factory
from jobfucker.clients.mock.params import (
    MockApplyBehaviorConfig,
    MockBehaviorConfig,
    MockOutcome,
    MockSearchEntry,
    MockSearchParams,
    MockServiceConfig,
)
from jobfucker.config import PipelineConfig
from jobfucker.stages.apply import ApplyFilters, ApplyReport, ApplyTargets, run_apply
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline
from jobfucker.storage.models import AuditLog
from test.fakes.fake_client import FakeApplyBehavior, FakeBehavior, FakeClient, FakeServiceConfig
from test.pipeline_helpers import build_pipeline_config, create_pipeline, make_factory, snapshot_for
from test.storage.builders import build_vacancy

TODAY = "2026-08-05"
_AUTH = "login@example.com"
_SERVICE = "mock"
_STRICT_FILTERS = ApplyFilters()


def _ids(*values: str) -> set[ServiceVacancyId]:
    return {ServiceVacancyId(v) for v in values}


def _params() -> MockSearchParams:
    """Build the mock's concrete search filter for a section."""
    return MockSearchParams(
        area=(1,),
        schedule=("fullDay",),
        experience="between1And3",
        only_with_salary=True,
    )


def _section(behavior: MockBehaviorConfig) -> MockServiceConfig:
    """Build a mock service section carrying the given behavior seam."""
    return MockServiceConfig(
        resume_id="mock-resume-1", searches=(MockSearchEntry(query="python", filter=_params()),), behavior=behavior
    )


def _outcome(outcome: MockOutcome, message: str | None = None) -> MockApplyBehaviorConfig:
    """A single programmable apply outcome for a ``MockBehaviorConfig``."""
    return MockApplyBehaviorConfig(outcome=outcome, message=message)


def _client(factory: Factory) -> Client:
    return factory.get("mock")


def _targets(factory: Factory, config: PipelineConfig) -> ApplyTargets:
    return ApplyTargets(client=_client(factory), resume_id=config.service_section.resume_id)


async def _seed_eligible(storage: Storage, pipeline: Pipeline, count: int) -> None:
    """Seed ``count`` scored, cover-lettered, pending vacancies at positions 0..n."""
    for index in range(count):
        await storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id=f"v{index}",
                score=5,
                score_reasoning="good",
                cover_letter=f"letter {index}",
                apply_status=None,
            )
        )


async def _apply(
    storage: Storage,
    pipeline: Pipeline,
    factory: Factory,
    config: PipelineConfig,
    *,
    filters: ApplyFilters = _STRICT_FILTERS,
    today: str = TODAY,
):
    """Run ``run_apply`` against the pipeline's head snapshot (config-from-DB).

    Thresholds + credentials come from the resolved snapshot — the same source a
    real ``--pipeline-id`` run uses — so the stage never reads them from the
    identity DTO.
    """
    snapshot = await snapshot_for(storage, pipeline)
    return await run_apply(
        storage,
        pipeline,
        _targets(factory, config),
        snapshot_id=snapshot.id,
        min_required_score=snapshot.min_required_score,
        daily_apply_limit=snapshot.daily_apply_limit,
        login=snapshot.login,
        service=snapshot.service,
        filters=filters,
        today=today,
    )


async def _audit_entries(storage: Storage, pipeline_id: int) -> list[AuditLog]:
    async with storage.session_factory() as session:
        rows = (await session.execute(select(AuditLog).where(AuditLog.pipeline_id == pipeline_id))).scalars().all()
        return list(rows)


async def test_apply_applied_outcome_sets_status_and_increments_counter(
    storage: Storage, client_deps: ClientDeps
) -> None:
    """A successful apply sets applied + applied_at and ticks the shared counter."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    snapshot = await snapshot_for(storage, pipeline)
    await _seed_eligible(storage, pipeline, 2)

    result = await _apply(storage, pipeline, make_factory(client_deps), config)
    assert result.is_ok
    report: ApplyReport = result.unwrap()
    assert report.applied == 2
    assert report.limit_reached is False
    assert report.checked == 2  # nothing ineligible in this pipeline
    assert report.ignored == ()

    stored = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    for key in _ids("v0", "v1"):
        assert stored[key].apply_status == "applied"
        assert stored[key].apply_error is None
        assert stored[key].applied_at is not None
        # The apply row is stamped with the snapshot whose config applied it.
        assert stored[key].applied_snapshot_id == snapshot.id

    # One shared counter per (service, login, date) ticked twice.
    limit_row = await storage.daily_limits.get(_SERVICE, _AUTH, TODAY)
    assert limit_row is not None
    assert limit_row.count == 2


async def test_apply_redirect_maps_to_skipped(storage: Storage, client_deps: ClientDeps) -> None:
    """A skip/redirect outcome → apply_status 'skipped' (never 'rejected')."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    await _seed_eligible(storage, pipeline, 1)
    behavior = MockBehaviorConfig(default_apply=_outcome("skipped", message="redirect to external form"))

    result = await _apply(storage, pipeline, make_factory(client_deps, section=_section(behavior)), config)
    assert result.is_ok
    assert result.unwrap().skipped == 1

    stored = (await storage.vacancies.list_by_pipeline(pipeline.id))[0]
    assert stored.apply_status == "skipped"
    # The board's skip text lands in skip_reason — never in apply_error.
    assert stored.skip_reason == "redirect to external form"
    assert stored.apply_error is None
    assert stored.applied_at is None


async def test_apply_per_vacancy_error_does_not_abort_batch(storage: Storage, client_deps: ClientDeps) -> None:
    """A per-vacancy ClientError → apply_status 'error' + apply_error; batch continues."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    await _seed_eligible(storage, pipeline, 2)
    behavior = MockBehaviorConfig(
        default_apply=_outcome("applied"),
        per_vacancy={"v0": _outcome("error", message="board rejected")},
    )

    result = await _apply(storage, pipeline, make_factory(client_deps, section=_section(behavior)), config)
    assert result.is_ok
    report = result.unwrap()
    assert report.failed == 1
    assert report.applied == 1
    assert report.limit_reached is False

    stored = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    assert stored[ServiceVacancyId("v0")].apply_status == "error"
    assert stored[ServiceVacancyId("v0")].apply_error is not None
    assert stored[ServiceVacancyId("v1")].apply_status == "applied"


async def test_apply_limit_exceeded_stops_ok_and_leaves_rest_pending(storage: Storage, client_deps: ClientDeps) -> None:
    """LimitExceededError → stop; Ok report with limit_reached; remaining pending."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    await _seed_eligible(storage, pipeline, 3)
    behavior = MockBehaviorConfig(
        default_apply=_outcome("applied"),
        per_vacancy={"v1": _outcome("limit_exceeded", message="cap reached")},
    )

    result = await _apply(storage, pipeline, make_factory(client_deps, section=_section(behavior)), config)
    assert result.is_ok  # a limit stop is an Ok, never an error
    report = result.unwrap()
    assert report.limit_reached is True
    assert report.stopped_early is True
    assert report.applied == 1  # v0 applied before the stop
    assert report.pending == 2  # v1 + v2 left pending
    # The board's own cap signalled the stop — the report wording says so.
    assert report.stop_message == "board signalled the daily limit"

    stored = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    assert stored[ServiceVacancyId("v2")].apply_status is None  # never attempted


async def test_apply_per_pipeline_limit_stops_before_next_apply(storage: Storage, client_deps: ClientDeps) -> None:
    """Reaching daily_apply_limit stops applying with limit_reached, Ok."""
    pipeline = await create_pipeline(storage, daily_apply_limit=1)
    config = build_pipeline_config(daily_apply_limit=1)
    await _seed_eligible(storage, pipeline, 3)

    result = await _apply(storage, pipeline, make_factory(client_deps), config)
    assert result.is_ok
    report = result.unwrap()
    assert report.applied == 1
    assert report.limit_reached is True
    assert report.pending == 2
    # The stop line names the pipeline cap with current/limit numbers.
    assert report.stop_message == "daily limit reached — 1 of 1 applied today (pipeline cap)"


async def test_apply_full_shared_counter_stops_before_first_apply(storage: Storage, client_deps: ClientDeps) -> None:
    """A counter already at the cap (another pipeline spent it) stops with zero attempts."""
    pipeline = await create_pipeline(storage, daily_apply_limit=1)
    config = build_pipeline_config(daily_apply_limit=1)
    await _seed_eligible(storage, pipeline, 3)
    # Another pipeline on the same account already used the single slot today.
    await storage.daily_limits.increment(_SERVICE, _AUTH, TODAY)

    result = await _apply(storage, pipeline, make_factory(client_deps), config)
    assert result.is_ok
    report = result.unwrap()
    assert report.applied == 0 and report.skipped == 0 and report.failed == 0
    assert report.pending == 3  # every candidate left untouched
    assert report.limit_reached is True
    assert report.stopped_early is True
    assert report.stop_message == "daily limit reached — 1 of 1 applied today (pipeline cap)"
    stored = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    assert all(v.apply_status is None for v in stored.values())


async def test_apply_report_buckets_partition_the_selection(storage: Storage, client_deps: ClientDeps) -> None:
    """Every vacancy in the window lands in exactly one bucket; buckets sum to checked."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    await _seed_eligible(storage, pipeline, 4)
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="noletter", score=5))
    behavior = MockBehaviorConfig(
        default_apply=_outcome("applied"),
        per_vacancy={"v0": _outcome("error", message="board rejected")},
    )

    result = await _apply(storage, pipeline, make_factory(client_deps, section=_section(behavior)), config)
    assert result.is_ok
    report = result.unwrap()
    assert (report.checked, report.total) == (5, 4)
    assert report.applied == 3 and report.skipped == 0 and report.failed == 1
    assert report.pending == 0 and len(report.ignored) == 1
    # The partition invariant: attempted + not attempted + not eligible == checked.
    assert report.applied + report.skipped + report.failed + report.pending + len(report.ignored) == report.checked
    assert report.stop_message is None


async def test_apply_mixed_filtered_and_limit_stopped_partition(storage: Storage, client_deps: ClientDeps) -> None:
    """A window mixing decided + eligible candidates under a full counter keeps the partition.

    Real-run regression shape: --take window = some already-decided vacancies
    (not eligible) + eligible candidates the full counter stops before — every
    one of them must surface in exactly one bucket.
    """
    pipeline = await create_pipeline(storage, daily_apply_limit=1)
    config = build_pipeline_config(daily_apply_limit=1)
    await _seed_eligible(storage, pipeline, 2)
    await storage.vacancies.upsert(
        build_vacancy(pipeline_id=pipeline.id, external_id="decided", score=5, cover_letter="l", apply_status="applied")
    )
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="gone",
            score=5,
            cover_letter="l",
            apply_status="skipped",
            skip_reason="closed",
        )
    )
    # The account's one slot is already spent today.
    await storage.daily_limits.increment(_SERVICE, _AUTH, TODAY)

    result = await _apply(storage, pipeline, make_factory(client_deps), config)
    assert result.is_ok
    report = result.unwrap()
    assert (report.checked, report.total) == (4, 2)
    assert report.applied == 0 and report.skipped == 0 and report.failed == 0
    assert report.pending == 2  # both eligible candidates stopped before attempts
    assert len(report.ignored) == 2  # both decided vacancies filtered out
    assert report.applied + report.skipped + report.failed + report.pending + len(report.ignored) == report.checked
    assert report.stop_message == "daily limit reached — 1 of 1 applied today (pipeline cap)"


async def test_apply_is_idempotent_no_double_apply(storage: Storage, client_deps: ClientDeps) -> None:
    """An already-applied vacancy is not re-applied to on a subsequent run."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    await _seed_eligible(storage, pipeline, 2)
    factory = make_factory(client_deps)
    # First run applies to everything.
    assert (await _apply(storage, pipeline, factory, config)).is_ok

    # Second run must not re-apply to the now-applied vacancies.
    result = await _apply(storage, pipeline, factory, config)
    assert result.is_ok
    assert result.unwrap().applied == 0
    limit_row = await storage.daily_limits.get(_SERVICE, _AUTH, TODAY)
    assert limit_row is not None and limit_row.count == 2  # unchanged


async def test_apply_skips_ineligible_vacancies(storage: Storage, client_deps: ClientDeps) -> None:
    """No cover letter / below threshold / manual_skip / unscored / decided — each reported with its reason."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="noletter", score=5))
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="below",
            score=2,
            cover_letter="letter",
        )
    )
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="manual",
            score=5,
            cover_letter="letter",
            manual_skip=True,
            manual_skip_reason="not interested",
        )
    )
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="manual_bare",
            score=5,
            cover_letter="letter",
            manual_skip=True,
        )
    )
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="unscored"))
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="already",
            score=5,
            cover_letter="letter",
            apply_status="applied",
        )
    )
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="skipped_earlier",
            score=5,
            cover_letter="letter",
            apply_status="skipped",
            skip_reason="vacancy closed on the board",
        )
    )

    result = await _apply(storage, pipeline, make_factory(client_deps), config)
    assert result.is_ok
    report = result.unwrap()
    assert report.total == 0  # no eligible candidates
    assert report.checked == 7
    reasons = {v.external_id: v.reason for v in report.ignored}
    assert reasons == {
        ServiceVacancyId("noletter"): "no cover letter (run generate first)",
        ServiceVacancyId("below"): "score 2 < required 3",
        ServiceVacancyId("manual"): "manually skipped: not interested",
        ServiceVacancyId("manual_bare"): "manually skipped",
        ServiceVacancyId("unscored"): "not scored yet (run score first)",
        ServiceVacancyId("already"): "applied in an earlier run",
        ServiceVacancyId("skipped_earlier"): "declined in an earlier run: vacancy closed on the board",
    }


async def test_apply_ignore_reason_reports_root_cause_not_downstream_status(
    storage: Storage, client_deps: ClientDeps
) -> None:
    """A sub-threshold vacancy reports its score as the root cause.

    The score stage never stamps ``apply_status`` (sub-threshold is derived
    from ``score`` vs the threshold), so a sub-threshold row stays pending —
    and the apply stage names the real gate: the score.
    """
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="sub",
            score=2,
            cover_letter="letter",
        )
    )

    result = await _apply(storage, pipeline, make_factory(client_deps), config)
    assert result.is_ok
    ignored = result.unwrap().ignored
    assert len(ignored) == 1
    assert ignored[0].reason == "score 2 < required 3"


async def test_apply_error_reason_carries_board_message(storage: Storage, client_deps: ClientDeps) -> None:
    """A previously-errored vacancy is ignored with the stored apply_error."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="broken",
            score=5,
            cover_letter="letter",
            apply_status="error",
            apply_error="board rejected",
        )
    )

    result = await _apply(storage, pipeline, make_factory(client_deps), config)
    assert result.is_ok
    ignored = result.unwrap().ignored
    assert len(ignored) == 1
    assert ignored[0].reason == "failed in an earlier run: board rejected"


async def test_apply_include_unscored_makes_unscored_eligible(storage: Storage, client_deps: ClientDeps) -> None:
    """``include_unscored`` relaxes the score gate; a lettered unscored vacancy is applied."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    await storage.vacancies.upsert(
        build_vacancy(pipeline_id=pipeline.id, external_id="unscored", cover_letter="letter")
    )
    factory = make_factory(client_deps)

    strict = await _apply(storage, pipeline, factory, config)
    assert strict.is_ok
    assert strict.unwrap().total == 0

    relaxed = await _apply(storage, pipeline, factory, config, filters=ApplyFilters(include_unscored=True))
    assert relaxed.is_ok
    report = relaxed.unwrap()
    assert report.applied == 1
    assert report.ignored == ()


async def test_apply_min_score_overrides_threshold_for_this_run(storage: Storage, client_deps: ClientDeps) -> None:
    """``min_score`` replaces the config threshold for the run (both directions)."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()  # config threshold is 3
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="sub", score=2, cover_letter="l"))
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="ok", score=4, cover_letter="l"))
    factory = make_factory(client_deps)

    raised = await _apply(storage, pipeline, factory, config, filters=ApplyFilters(min_score=5))
    assert raised.is_ok
    reasons = {v.external_id: v.reason for v in raised.unwrap().ignored}
    assert reasons == {
        ServiceVacancyId("sub"): "score 2 < required 5",
        ServiceVacancyId("ok"): "score 4 < required 5",
    }

    lowered = await _apply(storage, pipeline, factory, config, filters=ApplyFilters(min_score=2))
    assert lowered.is_ok
    assert lowered.unwrap().applied == 2


async def test_apply_allow_without_letter_applies_letterless(storage: Storage, client_deps: ClientDeps) -> None:
    """``allow_without_letter`` relaxes the letter gate; the letterless vacancy is applied."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="noletter", score=5))
    factory = make_factory(client_deps)

    strict = await _apply(storage, pipeline, factory, config)
    assert strict.is_ok
    assert strict.unwrap().total == 0

    relaxed = await _apply(storage, pipeline, factory, config, filters=ApplyFilters(allow_without_letter=True))
    assert relaxed.is_ok
    assert relaxed.unwrap().applied == 1
    stored = (await storage.vacancies.list_by_pipeline(pipeline.id))[0]
    assert stored.apply_status == "applied"
    assert stored.cover_letter is None  # applied with message=None (contract-valid letterless apply)


async def test_apply_only_ids_restrict_and_bypass_filters(storage: Storage, client_deps: ClientDeps) -> None:
    """``only_external_ids`` selects exactly the named ids and bypasses
    manual-skip/score/letter gates for them; the decided gate stays; other
    vacancies are never touched."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="forced_manual",
            score=5,
            cover_letter="l",
            manual_skip=True,
            manual_skip_reason="user said no",
        )
    )
    await storage.vacancies.upsert(
        build_vacancy(pipeline_id=pipeline.id, external_id="forced_sub", score=1, cover_letter="l")
    )
    await storage.vacancies.upsert(build_vacancy(pipeline_id=pipeline.id, external_id="forced_noletter", score=5))
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id, external_id="forced_applied", score=5, cover_letter="l", apply_status="applied"
        )
    )
    await storage.vacancies.upsert(
        build_vacancy(pipeline_id=pipeline.id, external_id="bystander", score=5, cover_letter="l")
    )
    factory = make_factory(client_deps)
    filters = ApplyFilters(
        only_external_ids=frozenset({"forced_manual", "forced_sub", "forced_noletter", "forced_applied"})
    )

    result = await _apply(storage, pipeline, factory, config, filters=filters)
    assert result.is_ok
    report = result.unwrap()
    assert report.applied == 3
    assert report.checked == 4  # only the forced ids are in the selection
    reasons = {v.external_id: v.reason for v in report.ignored}
    assert reasons == {ServiceVacancyId("forced_applied"): "applied in an earlier run"}

    stored = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    assert stored[ServiceVacancyId("bystander")].apply_status is None  # never part of an ids-run selection
    for key in _ids("forced_manual", "forced_sub", "forced_noletter"):
        assert stored[key].apply_status == "applied"


async def test_apply_force_decided_re_attempts_a_decided_vacancy(storage: Storage, client_deps: ClientDeps) -> None:
    """``force_decided`` + ids bypasses the decided gate: a previously-errored vacancy is re-attempted."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    await storage.vacancies.upsert(
        build_vacancy(
            pipeline_id=pipeline.id,
            external_id="broken",
            score=5,
            cover_letter="l",
            apply_status="error",
            apply_error="transient board failure",
            skip_reason="stale skip text from an older decision",
        )
    )
    factory = make_factory(client_deps)

    without_force = await _apply(
        storage, pipeline, factory, config, filters=ApplyFilters(only_external_ids=frozenset({"broken"}))
    )
    assert without_force.is_ok
    assert without_force.unwrap().total == 0  # decided gate holds by default

    with_force = await _apply(
        storage,
        pipeline,
        factory,
        config,
        filters=ApplyFilters(only_external_ids=frozenset({"broken"}), force_decided=True),
    )
    assert with_force.is_ok
    assert with_force.unwrap().applied == 1
    stored = (await storage.vacancies.list_by_pipeline(pipeline.id))[0]
    assert stored.apply_status == "applied"
    assert stored.apply_error is None
    # The opposite decision detail is cleared on the transition, not left stale.
    assert stored.skip_reason is None


def test_apply_filters_force_decided_requires_ids() -> None:
    """``force_decided`` without ids is a domain invariant violation (mass re-apply guard)."""
    with pytest.raises(ValueError, match="requires only_external_ids"):
        ApplyFilters(force_decided=True)


async def test_apply_writes_an_audit_entry(storage: Storage, client_deps: ClientDeps) -> None:
    """Apply appends one 'apply' audit_log entry with report counts + snapshot."""
    pipeline = await create_pipeline(storage)
    config = build_pipeline_config()
    snapshot = await snapshot_for(storage, pipeline)
    await _seed_eligible(storage, pipeline, 1)
    assert (await _apply(storage, pipeline, make_factory(client_deps), config)).is_ok

    entries = await _audit_entries(storage, pipeline.id)
    assert [entry.action for entry in entries] == ["apply"]
    assert '"applied": 1' in (entries[0].details or "")
    assert entries[0].pipeline_snapshot_id == snapshot.id


async def test_apply_shared_counter_across_pipelines_same_login(storage: Storage, client_deps: ClientDeps) -> None:
    """SC4: two pipelines on one login exhaust ONE shared daily counter.

    Both pipelines run under the same ``(service, login, date)`` key; their
    applications accumulate on a single row — the account cap is never
    double-counted per pipeline.
    """
    config = build_pipeline_config()
    factory = make_factory(client_deps)
    pipeline_a = await create_pipeline(storage, name="pipeline-a", login=_AUTH)
    pipeline_b = await create_pipeline(storage, name="pipeline-b", login=_AUTH)
    await _seed_eligible(storage, pipeline_a, 2)
    await _seed_eligible(storage, pipeline_b, 1)

    result_a = await _apply(storage, pipeline_a, factory, config)
    result_b = await _apply(storage, pipeline_b, factory, config)
    assert result_a.is_ok and result_b.is_ok
    assert result_a.unwrap().applied == 2
    assert result_b.unwrap().applied == 1

    # Exactly ONE shared (service, login, date) row holding the combined count.
    rows = await storage.daily_limits.list()
    assert len(rows) == 1
    assert rows[0].service == _SERVICE
    assert rows[0].login == _AUTH
    assert rows[0].date == TODAY
    assert rows[0].count == 3

    # Every applied vacancy is stamped with its own pipeline's snapshot id.
    snap_a = await snapshot_for(storage, pipeline_a)
    snap_b = await snapshot_for(storage, pipeline_b)
    for vacancy in await storage.vacancies.list_by_pipeline(pipeline_a.id):
        assert vacancy.applied_snapshot_id == snap_a.id
    for vacancy in await storage.vacancies.list_by_pipeline(pipeline_b.id):
        assert vacancy.applied_snapshot_id == snap_b.id


async def _run_with_fake_outcome(
    storage: Storage,
    pipeline: Pipeline,
    client_deps: ClientDeps,
    outcome: Literal["config_error", "auth_error"],
) -> Result[ApplyReport, str]:
    """Run the apply stage against a fake client scripted with one fatal outcome."""
    client = FakeClient(
        client_deps,
        FakeServiceConfig(resume_id="fake-resume-1"),  # type: ignore[arg-type]  # rationale: unregistered dataclass double, deliberately outside the protocol
        behavior=FakeBehavior(default_apply=FakeApplyBehavior(outcome)),
    )
    snapshot = await snapshot_for(storage, pipeline)
    return await run_apply(
        storage,
        pipeline,
        ApplyTargets(client=client, resume_id="fake-resume-1"),
        snapshot_id=snapshot.id,
        min_required_score=snapshot.min_required_score,
        daily_apply_limit=snapshot.daily_apply_limit,
        login=snapshot.login,
        service="fake",
        today=TODAY,
    )


async def _assert_fatal_outcome_stops_batch(
    storage: Storage,
    pipeline: Pipeline,
    result: Result[ApplyReport, str],
) -> None:
    """A fatal ClientError stops the batch: Ok report, rest pending, no error rows."""
    assert result.is_ok  # a fatal stop is an Ok, never an error
    report = result.unwrap()
    assert report.applied == 0
    assert report.failed == 0
    assert report.pending == 3
    assert report.stopped_early is True
    stored = {v.external_id: v for v in await storage.vacancies.list_by_pipeline(pipeline.id)}
    assert all(v.apply_status is None for v in stored.values())  # nothing marked error


async def test_apply_configuration_error_stops_batch(storage: Storage, client_deps: ClientDeps) -> None:
    """ConfigurationError (e.g. board rejected the configured resume) stops the pipeline."""
    pipeline = await create_pipeline(storage)
    await _seed_eligible(storage, pipeline, 3)
    result = await _run_with_fake_outcome(storage, pipeline, client_deps, "config_error")
    await _assert_fatal_outcome_stops_batch(storage, pipeline, result)


async def test_apply_auth_error_stops_batch(storage: Storage, client_deps: ClientDeps) -> None:
    """AuthError stops the pipeline (re-auth is needed, retries cannot help)."""
    pipeline = await create_pipeline(storage)
    await _seed_eligible(storage, pipeline, 3)
    result = await _run_with_fake_outcome(storage, pipeline, client_deps, "auth_error")
    await _assert_fatal_outcome_stops_batch(storage, pipeline, result)
