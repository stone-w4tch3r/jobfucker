"""Unit coverage for the search-preview view model (``app/search_view.py``).

Pure presentation logic: the DB status vocabulary (pinned to ``VacancyRecord``
fields with first-match-wins priority), salary rendering, and the text output
shape. The CLI BDD suite proves the wiring; this module pins the words.
"""

from __future__ import annotations

from typing import Any

import pytest

from jobfucker.app.search_view import PreviewContext, build_preview, db_status_of, render_text_preview, salary_text
from jobfucker.clients.base import Salary, SearchListing, ServiceVacancyId, VacancyShort
from jobfucker.storage.dto import Pipeline
from test.storage.builders import make_vacancy


@pytest.mark.unit
def test_db_status_none_record_is_new() -> None:
    """No stored row: the vacancy is new work."""
    assert db_status_of(None) == "new"


@pytest.mark.unit
def test_db_status_soft_deleted_beats_everything() -> None:
    """A soft-deleted row reads `deleted` no matter what artifacts it carries."""
    record = make_vacancy(soft_deleted_at="2026-08-31 00:00:00", apply_status="applied", score=4)
    assert db_status_of(record) == "deleted"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"apply_status": "applied"}, "applied"),
        ({"apply_status": "pending"}, "pending"),
        ({"apply_status": "skipped"}, "skipped"),
        ({"apply_status": "error"}, "error"),
        ({"manual_skip": True}, "manual-skip"),
        ({"cover_letter": "letter text"}, "lettered"),
        ({"score": 4}, "scored"),
        ({}, "fetched"),
    ],
)
def test_db_status_first_match_wins(
    overrides: dict[str, Any],  # lint-ignore[raw-dict]: test param tables  # lint-ignore[restricted-object]: payload
    expected: str,
) -> None:
    """The vocabulary table, in priority order; the most decisive state wins."""
    base = {"apply_status": None, "manual_skip": False, "cover_letter": None, "score": None, "soft_deleted_at": None}
    record = make_vacancy(**{**base, **overrides})  # type: ignore[arg-type]  # rationale: exact DTO field values verified by the frozen DTO
    assert db_status_of(record) == expected


@pytest.mark.unit
def test_db_status_apply_status_beats_manual_skip_and_letter() -> None:
    """An apply outcome is more decisive than a manual skip or a cover letter."""
    record = make_vacancy(apply_status="skipped", manual_skip=True, cover_letter="text")
    assert db_status_of(record) == "skipped"


@pytest.mark.unit
def test_salary_text_covers_all_shapes() -> None:
    """Both bounds, single bounds, currency-only, and absent."""
    assert salary_text(None) == "—"
    assert salary_text(Salary(from_=250000, to=350000, currency="RUR", gross=False)) == "250000-350000 RUR"
    assert salary_text(Salary(from_=250000, to=None, currency="RUR", gross=False)) == "250000+ RUR"
    assert salary_text(Salary(from_=None, to=350000, currency="RUR", gross=False)) == "≤350000 RUR"
    assert salary_text(Salary(from_=None, to=None, currency="RUR", gross=False)) == "RUR"
    assert salary_text(Salary(from_=None, to=None, currency=None, gross=False)) == "—"


def _pipeline() -> Pipeline:
    return Pipeline(
        id=3,
        name="demo",
        description=None,
        current_snapshot_id=None,
        created_at="",
        updated_at="",
        soft_deleted_at=None,
    )


def _context() -> PreviewContext:
    return PreviewContext(
        pipeline=_pipeline(),
        query="python",
        query_source="pipeline",
        params_source="pipeline filters (no override)",
        board_cap=2000,
    )


def _listing(
    *items: VacancyShort, found: int | None = 1, ui_url: str | None = "https://example.test/search?x"
) -> SearchListing:
    return SearchListing(
        items=items,
        offset=0,
        pages_scanned=1,
        pages_planned=1,
        exhausted=True,
        found=found,
        ui_url=ui_url,
    )


@pytest.mark.unit
def test_text_rendering_renders_header_table_and_footer() -> None:
    """A smoke check of the text form shape (full wiring proven by the CLI suite)."""
    item = VacancyShort(
        external_id=ServiceVacancyId("v-1"),
        title="Python Backend",
        url="https://example.test/v/1",
        company="ACME",
        salary=Salary(from_=100000, to=200000, currency="RUR", gross=False),
        area="Москва",
        published_at="2026-08-30T12:00:00+0300",
        snippet_requirement=None,
        snippet_responsibility=None,
    )
    preview = build_preview(_listing(item), _context(), {})
    rendered = render_text_preview(preview)
    assert "pipeline: demo (#3)" in rendered
    assert "found:    1   pages scanned: 1   exhausted: yes" in rendered  # 1 < cap: no cap note
    assert "web:      https://example.test/search?x" in rendered
    assert "1 vacancy, 1 page(s) scanned." in rendered
    assert "Python Backend" in rendered
    assert "2026-08-30" in rendered
    assert "100000-200000 RUR" in rendered


@pytest.mark.unit
def test_text_rendering_flags_the_board_cap_when_found_hits_it() -> None:
    """`found` at the cap carries the cap note (HH caps accessible results at 2000)."""
    preview = build_preview(_listing(found=2000), _context(), {})
    assert "found:    2000 (board cap: 2000)" in render_text_preview(preview)


@pytest.mark.unit
def test_text_rendering_without_results_is_self_explanatory() -> None:
    """An empty listing explains itself instead of printing an empty table."""
    preview = build_preview(
        _listing(found=0, ui_url=None),
        PreviewContext(
            pipeline=_pipeline(),
            query="python",
            query_source="--query",
            params_source="inline",
            board_cap=2000,
        ),
        {},
    )
    rendered = render_text_preview(preview)
    assert "No vacancies matched. Loosen the query or filters." in rendered
    assert "web:" not in rendered
