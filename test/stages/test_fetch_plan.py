"""Fetch slicing: pure window/slice planning coverage (``fetch_plan.py``).

Unit-tests the validation matrix and the window/slice resolution from the
fetch-slicing semantics (``docs/specs/jobfucker.md``; history:
``docs/implementation-plans/2026-08-18-client-side-fetch-slicing.md``) — pure,
no storage or client involved. The slice→native-pages mapping is client-side
mechanics covered in ``test/clients/test_paging.py``.
"""

from __future__ import annotations

import pytest

from jobfucker.clients.base import SearchWindow
from jobfucker.stages.fetch_plan import plan_fetch


def _ok(params: SearchWindow) -> tuple[int, int, int]:
    result = plan_fetch(params, max_search_items=None)
    assert result.is_ok
    plan = result.unwrap()
    return (plan.slice_start, plan.slice_end, plan.page_size)


@pytest.mark.unit
def test_validation_matrix_rejects_each_invalid_input() -> None:
    """Rules 1-10 of the matrix: one Err with the documented message each."""
    cases = (
        (SearchWindow(page_size=25), "--page-size must be one of 20, 50, 100"),
        (SearchWindow(take_pages=0), "--take-pages must be >= 1"),
        (SearchWindow(first_page=-1), "--first-page must be >= 0"),
        (SearchWindow(from_=-1), "--from must be >= 0"),
        (SearchWindow(to=-1), "--to must be >= 0"),
        (SearchWindow(take=0), "--take must be >= 1"),
        (SearchWindow(to=5, take=5), "Cannot combine --to with --take"),
        (SearchWindow(first_page=1, from_=5), "Cannot combine --first-page with --from/--to"),
        (SearchWindow(first_page=1, to=5), "Cannot combine --first-page with --from/--to"),
        (SearchWindow(from_=5, to=2), "--to must be >= --from"),
        (SearchWindow(take=1001), "--take 1001 exceeds the fetch window (1000 vacancies); raise --take-pages"),
        (
            SearchWindow(take=201, page_size=20, take_pages=1),
            "--take 201 exceeds the fetch window (20 vacancies); raise --take-pages",
        ),
        (SearchWindow(from_=1000), "--from 1000 is outside the fetch window [0, 1000)"),
        (SearchWindow(from_=1000, to=1001), "--from 1000 is outside the fetch window [0, 1000)"),
        (SearchWindow(to=1000), "--to 1000 is outside the fetch window [0, 1000)"),
    )
    for params, message in cases:
        result = plan_fetch(params, max_search_items=None)
        assert result.is_err
        assert result.unwrap_err() == message


@pytest.mark.unit
def test_window_cap_respects_service_max_search_items() -> None:
    """Rule 7: the window must fit inside the service's searchable cap."""
    result = plan_fetch(SearchWindow(first_page=15, take_pages=10, page_size=100), max_search_items=2000)
    assert result.is_err
    assert result.unwrap_err() == (
        "fetch window [1500, 2500) exceeds the service's maximum searchable items (2000); "
        "reduce --take-pages or --page-size"
    )

    at_cap = plan_fetch(SearchWindow(first_page=10, take_pages=10, page_size=100), max_search_items=2000)
    assert at_cap.is_ok
    assert at_cap.unwrap().window_end == 2000


@pytest.mark.unit
def test_no_cap_means_unlimited_window() -> None:
    """A ``None`` cap (mock/fake services) never rejects on window size."""
    result = plan_fetch(SearchWindow(first_page=100, take_pages=10, page_size=100), max_search_items=None)
    assert result.is_ok
    assert result.unwrap().window_end == 11000


@pytest.mark.unit
def test_slice_resolution_table() -> None:
    """The flag→slice table: what the resolved (slice_start, slice_end) is."""
    cases = (
        (SearchWindow(), (0, 1000)),
        (SearchWindow(take=97), (0, 97)),
        (SearchWindow(take=202), (0, 202)),
        (SearchWindow(from_=101, take=10), (101, 111)),
        (SearchWindow(to=99), (0, 100)),
        (SearchWindow(from_=101, to=250), (101, 251)),
        (SearchWindow(from_=300), (300, 1000)),
        (SearchWindow(first_page=2, take=50), (200, 250)),
        (SearchWindow(first_page=2), (200, 1200)),
        (SearchWindow(first_page=2, take_pages=3, page_size=50), (100, 250)),
    )
    for params, (slice_start, slice_end) in cases:
        assert _ok(params)[:2] == (slice_start, slice_end)


@pytest.mark.unit
def test_window_bounds_follow_first_page_and_take_pages() -> None:
    """The window is [first_page*page_size, (first_page + take_pages)*page_size)."""
    result = plan_fetch(SearchWindow(first_page=2, page_size=50, take_pages=4), max_search_items=None)
    assert result.is_ok
    plan = result.unwrap()
    assert (plan.window_start, plan.window_end) == (100, 300)
    assert plan.page_size == 50


@pytest.mark.unit
def test_take_from_first_page_starts_at_the_window_start() -> None:
    """``--first-page P --take K`` slices [W.start, W.start + K), not [0, K)."""
    assert _ok(SearchWindow(first_page=2, take=50)) == (200, 250, 100)
