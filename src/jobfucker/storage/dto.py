"""Frozen domain DTOs for the persistence layer.

These are the **only** types engine / CLI / UI ever see from storage's five
tables (``pipelines``, ``pipeline_snapshot``, ``vacancies``, ``audit_log``,
``daily_limits``). They are deliberately:

- **frozen** dataclasses (immutable value objects), so callers cannot corrupt a
  row in place and forget to persist it;
- **ORM-free** — no ``sqlalchemy`` import in this module; the DTOs are pure
  Python mapped *from* ORM rows by the repositories in :mod:`db`;
- **board-neutral** — no board name anywhere; a listing id is the generic
  ``ServiceVacancyId`` (from :mod:`jobfucker.clients.base`), and ``service`` is
  only the client-selector string stored per snapshot.

Post-snapshot rewrite, :class:`Pipeline` is **identity only** and its config
lives in :class:`PipelineSnapshot`. Result rows (`vacancies`/`audit_log`) carry
the pipeline **identity** plus the **snapshot** whose config produced the
artifact via the ``*_snapshot_id`` fields. :class:`DailyLimit` is keyed per auth
``(service, login, date)``, shared across pipelines.

Dates mirror the DDL's storage: SQLite stores them as ``TEXT`` via
``datetime('now')`` (e.g. ``"2026-08-05 12:00:00"``) and the daily-limit ``date``
as ``"YYYY-MM-DD"``. All date-ish fields are therefore ``str`` here so nothing is
re-parsed or forced into a ``timezone``-aware object at the persistence boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jobfucker.clients.base import ServiceVacancyId

# --- Shared value types ------------------------------------------------------
# Closed set of apply outcomes. Mirrors the DDL CHECK exactly: `rejected` is
# deliberately NOT a member (a redirect/missed threshold collapses into
# `skipped`, never a bespoke `rejected` status).
ApplyStatus = Literal["pending", "applied", "skipped", "error"]


# --- pipelines (identity only) ----------------------------------------------
@dataclass(frozen=True, slots=True)
class Pipeline:
    """A stable pipeline **identity** row (frozen view of `pipelines`).

    Carries NO config: ``current_snapshot_id`` points at the head
    :class:`PipelineSnapshot`. The id is stable across updates (an update
    appends a snapshot; it never re-ids or soft-deletes this row).
    """

    id: int
    name: str
    description: str | None
    current_snapshot_id: int | None  # FK -> pipeline_snapshot.id; the "head"
    created_at: str
    updated_at: str
    soft_deleted_at: str | None


# --- pipeline_snapshot -------------------------------------------------------
@dataclass(frozen=True, slots=True)
class PipelineSnapshot:
    """An immutable config snapshot (frozen view of `pipeline_snapshot`).

    Carries the loaded file CONTENTS and opaque section JSON — everything needed
    to reconstruct a :class:`PipelineConfig` with zero file I/O. ``id``/``pipeline_id``/
    ``snapshot_no``/``source``/``note``/``created_at`` identify the snapshot; the
    remaining fields are the config content.
    """

    id: int
    pipeline_id: int
    snapshot_no: int  # 1,2,3... per pipeline (append-only)
    source: str  # 'from_file' | 'manual'
    note: str | None
    service: str  # client selector string ("mock", "hh", ...); storage stays board-neutral
    # Opaque JSON of the validated `service.<board>` section (resume_id + the
    # ordered search pool: per-entry query/window/board filter). Core stores it
    # as-is and never reads its internals.
    service_section: str | None
    # Optional AI-captcha block (model/base_url/api_key CONTENT)
    # as opaque JSON; `None` when no captcha section is configured.
    openai_captcha: str | None
    login: str  # auth login CONTENT (loaded once; never a path)
    password: str  # auth password CONTENT (loaded once; never a path)
    resume: str  # resume markdown CONTENT (loaded once; never a path)
    openai_model: str
    openai_base_url: str | None
    openai_api_key: str  # openai api_key CONTENT (`config.openai.api_key or ""`)
    min_required_score: int
    scoring_prompt: str  # scoring prompt template CONTENT
    apply_prompt: str  # apply prompt template CONTENT
    daily_apply_limit: int
    created_at: str
    # Optional reasoning_effort scalar from the ``openai`` section (None = not
    # configured/omitted). Defaulted so pre-existing construction sites (which
    # never passed it) keep working.
    openai_reasoning_effort: str | None = None
    # Optional hh screening-test solving block (enabled + prompt template
    # CONTENT) as opaque JSON, mirroring ``openai_captcha``; ``None`` when the
    # top-level ``hh_test_solving`` section is not configured. Defaulted so
    # pre-existing construction sites keep working.
    hh_test_solving: str | None = None


# --- vacancies ---------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class VacancyRecord:
    """A fetched/processed listing row (frozen view of `vacancies`)."""

    id: int
    pipeline_id: int  # identity (stable)
    fetched_snapshot_id: int | None  # query/service snapshot used to list
    scored_snapshot_id: int | None  # scoring cfg snapshot used
    generated_snapshot_id: int | None  # apply cfg snapshot used for cover
    applied_snapshot_id: int | None  # apply cfg snapshot used for apply
    external_id: ServiceVacancyId  # board listing id — typed, never a DB id
    title: str
    url: str
    company: str | None
    description: str
    salary: str | None
    score: int | None  # 1-5 scale; None = not scored
    score_reasoning: str | None
    score_error: str | None
    cover_letter: str | None
    cover_letter_error: str | None
    apply_status: ApplyStatus | None  # None = untouched by the apply stage
    apply_error: str | None  # why an apply attempt errored (error status only)
    skip_reason: str | None  # why the board skipped the apply attempt (skipped status only)
    applied_at: str | None
    # Staleness timestamps ("YYYY-MM-DD HH:MM:SS" UTC, same format as applied_at).
    # Each stage bumps its own on every write; manual mutations bump
    # user_edited_at. Computed staleness on read: fetched_at > scored_at /
    # generated_at / user_edited_at (NULL-safe).
    fetched_at: str | None
    scored_at: str | None
    generated_at: str | None
    user_edited_at: str | None
    manual_skip: bool
    manual_skip_reason: str | None
    notes: str | None
    created_at: str
    updated_at: str
    soft_deleted_at: str | None
    # hh test presence as reported at fetch time (NULL = board does not report
    # it or the row predates the column). Defaulted so pre-existing DTO
    # construction sites (tests, storage clients) keep working.
    has_hh_test: bool | None = None


# --- audit_log ---------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class AuditLogEntry:
    """A single operation-history row (frozen view of `audit_log`)."""

    id: int
    pipeline_id: int
    pipeline_snapshot_id: int | None  # snapshot whose config the op ran against
    action: str  # 'fetch', 'score', 'generate', 'apply', 'manual_skip', ...
    details: str | None  # JSON with action-specific metadata
    created_at: str


# --- daily_limits ------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DailyLimit:
    """A per-auth, per-day application counter row (frozen view of `daily_limits`).

    Keyed by ``(service, login, date)`` — shared across every pipeline using the
    account, so a cap is never double-counted per pipeline.
    """

    id: int
    service: str  # client selector ("mock", "hh", ...)
    login: str  # auth login identifying the account
    date: str  # "YYYY-MM-DD"
    count: int
    created_at: str
    updated_at: str
