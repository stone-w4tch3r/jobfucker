"""Pure fetch window/slice planning (fetch slicing).

Separate from the fetch stage: :func:`plan_fetch` validates a user's
window/slice intent (:class:`~jobfucker.clients.base.SearchWindow`) against the
service's search cap and resolves it into the exact position window, slice and
page size to request from the client. It is pure — no I/O, deterministic, and
unit-testable without storage or a client. All indexes are 0-based: a "page" is
a native board page, a "position" is an item index relative to page 0.

The ``SearchWindow`` model itself (fields + defaults + aliases) lives in
``jobfucker.clients.base`` next to the search-pool contract; this module owns
the **one** validation matrix over it. ``origin`` prefixes every error message
so the same matrix serves CLI flags (``--take must be >= 1``) and per-entry
config errors (``search 2: --take must be >= 1``).
"""

from __future__ import annotations

from dataclasses import dataclass

from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import ALLOWED_PAGE_SIZES, SearchWindow

__all__ = ["FetchPlan", "plan_fetch"]


@dataclass(frozen=True, slots=True)
class FetchPlan:
    """A validated, resolved fetch: the window, the slice and the page size.

    Which native pages the slice overlaps is the client-side driver's math
    (``jobfucker.clients.paging.plan_pages``) — the plan carries only the
    product-level window/slice resolution the stage needs.
    """

    window_start: int
    window_end: int
    slice_start: int
    slice_end: int
    page_size: int


def _validate_and_window(
    params: SearchWindow, max_search_items: int | None, origin: str
) -> Result[tuple[int, int], str]:
    """Rules 1-7 of the validation matrix; returns the window bounds on success.

    Every message names the offending flag so a CLI user can fix the command
    directly (a config error carries the same flag words prefixed by
    ``origin``). ``max_search_items`` is the service's searchable-listing cap
    (``None`` = no cap, e.g. the mock).
    """
    window_start = params.first_page * params.page_size
    window_end = window_start + params.take_pages * params.page_size
    checks = (
        (params.page_size not in ALLOWED_PAGE_SIZES, f"{origin}--page-size must be one of 20, 50, 100"),
        (params.take_pages < 1, f"{origin}--take-pages must be >= 1"),
        (params.first_page < 0, f"{origin}--first-page must be >= 0"),
        (params.from_ is not None and params.from_ < 0, f"{origin}--from must be >= 0"),
        (params.to is not None and params.to < 0, f"{origin}--to must be >= 0"),
        (params.take is not None and params.take < 1, f"{origin}--take must be >= 1"),
        (params.to is not None and params.take is not None, f"{origin}Cannot combine --to with --take"),
        (
            params.first_page != 0 and (params.from_ is not None or params.to is not None),
            f"{origin}Cannot combine --first-page with --from/--to",
        ),
        (
            params.from_ is not None and params.to is not None and params.to < params.from_,
            f"{origin}--to must be >= --from",
        ),
        (
            max_search_items is not None and window_end > max_search_items,
            f"{origin}fetch window [{window_start}, {window_end}) exceeds the service's "
            f"maximum searchable items ({max_search_items}); reduce --take-pages or --page-size",
        ),
    )
    for invalid, message in checks:
        if invalid:
            return Err(message)
    return Ok((window_start, window_end))


def plan_fetch(params: SearchWindow, *, max_search_items: int | None, origin: str = "") -> Result[FetchPlan, str]:
    """Validate ``params`` and resolve the slice + pages to load.

    The whole validation matrix (page sizes, flag combinations, window bounds)
    is checked before any I/O, then the slice is resolved inside the window and
    mapped onto the minimal set of native pages to load. ``origin`` prefixes
    every error (``""`` for CLI flag errors, ``"search N: "`` for a pipeline
    search entry's configured window).
    """
    window_result = _validate_and_window(params, max_search_items, origin)
    if window_result.is_err:
        return Err(window_result.unwrap_err())
    window_start, window_end = window_result.unwrap()

    slice_start = params.from_ if params.from_ is not None else window_start
    if params.take is not None:
        slice_end = slice_start + params.take
    elif params.to is not None:
        slice_end = params.to + 1
    else:
        slice_end = window_end

    if slice_start >= window_end:
        return Err(f"{origin}--from {params.from_} is outside the fetch window [{window_start}, {window_end})")
    if slice_end > window_end:
        if params.take is not None:
            return Err(
                f"{origin}--take {params.take} exceeds the fetch window "
                f"({window_end - window_start} vacancies); raise --take-pages"
            )
        return Err(f"{origin}--to {params.to} is outside the fetch window [{window_start}, {window_end})")

    return Ok(
        FetchPlan(
            window_start=window_start,
            window_end=window_end,
            slice_start=slice_start,
            slice_end=slice_end,
            page_size=params.page_size,
        )
    )
