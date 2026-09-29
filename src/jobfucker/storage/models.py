"""SQLAlchemy 2.0 declarative ORM models — the canonical jobfucker schema.

These ``Mapped`` models mirror the board-neutral tables in the frozen DDL
(``pipelines``, ``pipeline_snapshot``, ``vacancies``, ``audit_log``,
``auth_apply_limits``, ``pipeline_apply_limits``), including the ``apply_status``
CHECK (``'pending'|'applied'|'skipped'|'error'`` — **no** ``rejected``), the
unique constraints, and the ``soft_deleted_at`` soft-delete column.

Post-snapshot rewrite, ``pipelines`` is **identity only**: a stable, user-
recognized pipeline label (``id``/``name``/``description``/``current_snapshot_id``
plus lifecycle timestamps). Its config lives in the append-only
``pipeline_snapshot`` table; ``current_snapshot_id`` points at the "head"
snapshot whose config an engine run reconstructs with zero file I/O. Result
rows (``vacancies``/``audit_log``) carry the pipeline **identity** plus the
**snapshot** whose config produced the artifact via the ``*_snapshot_id``
columns. ``auth_apply_limits`` is keyed per auth
``(service, login, period, period_key)`` and ``pipeline_apply_limits`` per
``(pipeline_id, period, period_key)``; the two counters are independent, and the
period is the board's declared quota window (``"day"`` / ``"month"``).

This module is authoritative: the Alembic migrations in ``migrations/`` generate
from ``Base.metadata`` and repository tests create schema from it. SQLAlchemy is
confined to this package (ruff ``banned-api`` + per-file-ignores); nothing above
``storage/`` imports it.

The boolean-ish DDL flag (``manual_skip``) is declared ``INTEGER`` (0/1) to
match the DDL exactly; the repository converts it to ``bool`` on the frozen
DTO.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Constraint,
    ForeignKey,
    Integer,
    MetaData,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# naming_convention so Alembic autogenerate emits stable, diff-able constraint
# names (and down migrations can drop them by name).
_NAMING_CONVENTION: Final = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by all ORM models."""

    metadata = MetaData(naming_convention=_NAMING_CONVENTION)


# Defaults shared per-table so Python-side and DDL defaults agree.
_NOW = text("(datetime('now'))")


class Pipeline(Base):
    """A stable pipeline **identity** (pipeline-snapshots DDL `pipelines`).

    Intentionally carries NO config: ``current_snapshot_id`` points at the
    ``pipeline_snapshot`` holding the exact parameters. ``name`` is a unique
    user label among non-soft-deleted rows (enforced by the migration-only
    partial index ``uq_pipelines_name_active``, not a plain model unique, so
    soft-deleted rows may reuse a name).
    """

    __tablename__ = "pipelines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    current_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_snapshot.id"), nullable=True)
    # Lifecycle
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)
    soft_deleted_at: Mapped[str | None] = mapped_column(Text)


class PipelineSnapshot(Base):
    """An immutable config snapshot under a pipeline (pipeline-snapshots DDL).

    Append-only: one row per real config change, ordered by ``snapshot_no``
    (1,2,3... per pipeline). Carries the loaded file CONTENTS verbatim (auth,
    resume markdown, openai api_key, prompt templates) plus the opaque
    ``service_section`` / ``openai_captcha`` JSON — everything needed to
    reconstruct a :class:`PipelineConfig` with zero file I/O.
    """

    __tablename__ = "pipeline_snapshot"
    __table_args__: tuple[Constraint, ...] = (
        UniqueConstraint("pipeline_id", "snapshot_no", name="uq_pipeline_snapshot_pipeline_snapshot_no"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), nullable=False, index=True)
    snapshot_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="manual", server_default="manual")
    note: Mapped[str | None] = mapped_column(Text)
    service: Mapped[str] = mapped_column(Text, nullable=False, default="hh", server_default="hh")
    # Opaque JSON of the validated `service.<board>` section (resume_id + the
    # board's filter params). Core stores it as-is and never reads its internals.
    service_section: Mapped[str | None] = mapped_column(Text)
    # Optional AI-captcha block (model/base_url/api_key CONTENT)
    # stored as opaque JSON; ``None`` when no captcha section is configured.
    openai_captcha: Mapped[str | None] = mapped_column(Text)
    # Optional hh screening-test solving block (enabled + prompt template
    # CONTENT) stored as opaque JSON, mirroring ``openai_captcha``; ``None``
    # when the top-level ``hh_test_solving`` section is not configured.
    hh_test_solving: Mapped[str | None] = mapped_column(Text)
    # Auth credential CONTENTS (loaded once at init/update, never re-read).
    login: Mapped[str] = mapped_column(Text, nullable=False)
    password: Mapped[str] = mapped_column(Text, nullable=False)
    # Resume markdown CONTENTS (loaded once at init/update, never re-read).
    resume: Mapped[str] = mapped_column(Text, nullable=False)
    # OpenAI configuration (JSON-ish, stored as text per DDL)
    openai_model: Mapped[str] = mapped_column(Text, nullable=False)
    openai_base_url: Mapped[str | None] = mapped_column(Text)
    openai_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    # reasoning_effort from the openai section (scalar column like openai_model)
    openai_reasoning_effort: Mapped[str | None] = mapped_column(Text)
    # Scoring config
    min_required_score: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    # Scoring prompt template CONTENT.
    scoring_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # Apply prompt template CONTENT.
    apply_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    # Limits
    apply_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)


class Vacancy(Base):
    """Fetched listing with scoring / cover-letter / apply results (§3.1 `vacancies`).

    One row per ``(pipeline_id, external_id)``; the ``*_snapshot_id`` columns
    record which config snapshot produced each artifact. Fetch is an insert-only
    mirror sync (``--refresh`` overwrites listing data only), so ordering is by
    ``id`` — there is no ``position_index``. ``soft_deleted_at`` is the **only**
    lifecycle signal and is set manually (user hide/archive/delete), never by
    fetch.

    Staleness is computed from the ``*_at`` timestamps on read (NULL-safe):
    ``score_stale = fetched_at > scored_at``, ``letter_stale = fetched_at >
    generated_at``, ``user_stale = fetched_at > user_edited_at``. Each stage
    bumps its own timestamp on every write; manual mutation paths bump
    ``user_edited_at``.
    """

    __tablename__ = "vacancies"
    __table_args__: tuple[Constraint, ...] = (
        UniqueConstraint("pipeline_id", "external_id", name="uq_vacancies_pipeline_external"),
        CheckConstraint(
            "apply_status IN ('pending','applied','skipped','error')",
            name="apply_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), nullable=False, index=True)
    # Which config snapshot produced each artifact (null until that artifact).
    fetched_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_snapshot.id"), nullable=True, index=True
    )
    scored_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_snapshot.id"), nullable=True, index=True
    )
    generated_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_snapshot.id"), nullable=True, index=True
    )
    applied_snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_snapshot.id"), nullable=True, index=True
    )
    external_id: Mapped[str] = mapped_column(Text, nullable=False)  # listing id (ServiceVacancyId at DTO level)
    # Listing data (board-neutral)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    company: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    salary: Mapped[str | None] = mapped_column(Text)
    # Test presence flag (1/0/NULL). The single hh-named informational column on
    # an otherwise board-neutral table (recorded arch exception): the HH client
    # reports it from vacancy detail at fetch time so the apply stage and the
    # ``hh-tests`` CLI can select test-bearing vacancies without per-candidate
    # probing. Boards without the notion leave it NULL (mock, future clients).
    has_hh_test: Mapped[bool | None] = mapped_column(Integer)
    # AI scoring
    score: Mapped[int | None] = mapped_column(Integer)
    score_reasoning: Mapped[str | None] = mapped_column(Text)
    score_error: Mapped[str | None] = mapped_column(Text)
    # Cover letter
    cover_letter: Mapped[str | None] = mapped_column(Text)
    cover_letter_error: Mapped[str | None] = mapped_column(Text)
    # Apply tracking
    apply_status: Mapped[str | None] = mapped_column(Text)  # CHECK enforces the closed set
    apply_error: Mapped[str | None] = mapped_column(Text)  # why an apply attempt errored
    skip_reason: Mapped[str | None] = mapped_column(Text)  # why the board skipped the attempt
    applied_at: Mapped[str | None] = mapped_column(Text)
    # Staleness timestamps ("YYYY-MM-DD HH:MM:SS" UTC, same format as applied_at).
    # Each stage bumps its own; manual mutations bump user_edited_at. Computed
    # staleness on read: fetched_at > scored_at / generated_at / user_edited_at.
    fetched_at: Mapped[str | None] = mapped_column(Text)
    scored_at: Mapped[str | None] = mapped_column(Text)
    generated_at: Mapped[str | None] = mapped_column(Text)
    user_edited_at: Mapped[str | None] = mapped_column(Text)
    # Manual overrides (editable in the Qt app)
    manual_skip: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    manual_skip_reason: Mapped[str | None] = mapped_column(Text)
    # Observation (editable in the Qt app)
    notes: Mapped[str | None] = mapped_column(Text)
    # Lifecycle
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)
    soft_deleted_at: Mapped[str | None] = mapped_column(Text)


class AuditLog(Base):
    """Operation history for traceability (§3.1 `audit_log`)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), nullable=False, index=True)
    pipeline_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("pipeline_snapshot.id"), nullable=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)


class AuthApplyLimit(Base):
    """Per-auth application counter over the board's quota window (§3.1).

    Keyed by ``(service, login, period, period_key)`` — shared across every
    pipeline using the account (no ``pipeline_id``, so the board's cap is never
    double-counted per pipeline). ``period`` is ``"day"`` or ``"month"``;
    ``period_key`` is ``"YYYY-MM-DD"`` for a day window and ``"YYYY-MM"`` for a
    month window, so a window change is simply a new row.
    """

    __tablename__ = "auth_apply_limits"
    __table_args__: tuple[Constraint, ...] = (
        UniqueConstraint(
            "service", "login", "period", "period_key", name="uq_auth_apply_limits_service_login_period_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    service: Mapped[str] = mapped_column(Text, nullable=False, default="hh", server_default="hh")
    login: Mapped[str] = mapped_column(Text, nullable=False)  # auth login identifying the account
    period: Mapped[str] = mapped_column(Text, nullable=False, default="day", server_default="day")  # "day"|"month"
    period_key: Mapped[str] = mapped_column(Text, nullable=False)  # "YYYY-MM-DD" (day) | "YYYY-MM" (month)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)


class PipelineApplyLimit(Base):
    """Per-pipeline application counter over the board's quota window.

    Keyed by ``(pipeline_id, period, period_key)`` — this pipeline's own
    ``apply_limit`` throttle, independent of the shared per-auth counter. Same
    window vocabulary as :class:`AuthApplyLimit`.
    """

    __tablename__ = "pipeline_apply_limits"
    __table_args__: tuple[Constraint, ...] = (
        UniqueConstraint("pipeline_id", "period", "period_key", name="uq_pipeline_apply_limits_pipeline_period_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pipeline_id: Mapped[int] = mapped_column(ForeignKey("pipelines.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(Text, nullable=False, default="day", server_default="day")  # "day"|"month"
    period_key: Mapped[str] = mapped_column(Text, nullable=False)  # "YYYY-MM-DD" (day) | "YYYY-MM" (month)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)
    updated_at: Mapped[str] = mapped_column(Text, nullable=False, server_default=_NOW)
