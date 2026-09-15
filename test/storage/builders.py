"""Test data builders for the storage suites.

Small factories that construct frozen domain DTOs (from
:mod:`jobfucker.storage.dto`) with sensible defaults, so storage tests can
specify only the fields they care about. These exercise the real repository
path; they are storage-tests-only helpers (not shipped to ``src/``).

Post-snapshot rewrite the :class:`Pipeline` DTO is **identity only**; config
lives in :class:`PipelineSnapshot` (built by :func:`make_snapshot`).
"""

from __future__ import annotations

from dataclasses import replace

from jobfucker.clients.base import ServiceVacancyId
from jobfucker.storage.dto import ApplyStatus, DailyLimit, Pipeline, PipelineSnapshot, VacancyRecord

# Frozen default DTOs; `make_*` re-derives a copy with overrides applied.
_BASE_PIPELINE = Pipeline(
    id=0,
    name="demo",
    description=None,
    current_snapshot_id=None,
    created_at="2026-08-05 09:00:00",
    updated_at="2026-08-05 09:00:00",
    soft_deleted_at=None,
)

# A default config-content snapshot (mirrors the old full Pipeline config fields).
_BASE_SNAPSHOT = PipelineSnapshot(
    id=0,
    pipeline_id=0,
    snapshot_no=1,
    source="manual",
    note=None,
    service="mock",
    service_section=None,
    openai_captcha=None,
    login="user@example.com",
    password="s3cret",
    resume="resume contents",
    openai_model="gpt-test",
    openai_base_url="https://api.example.com/v1",
    openai_api_key="test-key",
    min_required_score=3,
    scoring_prompt="Score {{ vacancy_formatted }}",
    apply_prompt="Apply for {{ vacancy_formatted }}",
    daily_apply_limit=50,
    created_at="2026-08-05 09:00:00",
    openai_reasoning_effort=None,
)

_BASE_VACANCY = VacancyRecord(
    id=0,
    pipeline_id=0,
    fetched_snapshot_id=None,
    scored_snapshot_id=None,
    generated_snapshot_id=None,
    applied_snapshot_id=None,
    external_id=ServiceVacancyId("1"),
    title="Senior Backend",
    url="https://board.example/v/1",
    company="ACME",
    description="A full plaintext description.",
    salary=None,
    score=None,
    score_reasoning=None,
    score_error=None,
    cover_letter=None,
    cover_letter_error=None,
    apply_status=None,
    apply_error=None,
    skip_reason=None,
    applied_at=None,
    fetched_at=None,
    scored_at=None,
    generated_at=None,
    user_edited_at=None,
    manual_skip=False,
    manual_skip_reason=None,
    notes=None,
    created_at="2026-08-05 09:00:00",
    updated_at="2026-08-05 09:00:00",
    soft_deleted_at=None,
)


def make_pipeline(**overrides: object) -> Pipeline:
    """Build an identity-only :class:`Pipeline` DTO overriding the given fields."""
    return replace(_BASE_PIPELINE, **overrides)  # type: ignore[arg-type]  # rationale: object-typed overrides widen but values are verified by DTO construction


def make_snapshot(**overrides: object) -> PipelineSnapshot:
    """Build a :class:`PipelineSnapshot` DTO overriding the given fields."""
    return replace(_BASE_SNAPSHOT, **overrides)  # type: ignore[arg-type]  # rationale: object-typed overrides widen but values are verified by DTO construction


def make_vacancy(**overrides: object) -> VacancyRecord:
    """Build a :class:`VacancyRecord` DTO overriding the given fields."""
    return replace(_BASE_VACANCY, **overrides)  # type: ignore[arg-type]  # rationale: object-typed overrides widen but values are verified by DTO construction


def build_vacancy(
    *,
    pipeline_id: int,
    external_id: str,
    apply_status: ApplyStatus | None = None,
    **overrides: object,
) -> VacancyRecord:
    """Convenience wrapper binding identity fields required by the repositories."""
    return make_vacancy(
        pipeline_id=pipeline_id,
        external_id=ServiceVacancyId(external_id),
        apply_status=apply_status,
        **overrides,
    )


def make_limit(*, service: str = "mock", login: str, date: str, count: int = 0) -> DailyLimit:
    """Build an auth-keyed :class:`DailyLimit` DTO with the given identity fields."""
    return DailyLimit(
        id=0,
        service=service,
        login=login,
        date=date,
        count=count,
        created_at="2026-08-05 09:00:00",
        updated_at="2026-08-05 09:00:00",
    )
