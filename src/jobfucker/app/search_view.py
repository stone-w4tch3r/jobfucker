"""Search-preview view model: DB status words + output rendering.

The presentation glue for the ``jobfucker search`` command (CLI today, the
planned GUI reuses it): it joins a board :class:`~jobfucker.clients.base.
SearchListing` against the stored mirror (per-vacancy DB status) and renders
the result as text / JSON / YAML. No business logic lives here — the client
returns the listing, storage returns the rows, this module only derives the
one status word per vacancy and shapes the output.

The DB status vocabulary is derived, never stored: it answers, per search
result, whether the vacancy is new work or already handled (fetched, scored,
lettered, applied, skipped, ...). Soft-deleted rows read as ``deleted`` —
fetch never touches them, so ``new`` would mislead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jobfucker.clients.base import Salary, SearchListing
from jobfucker.storage.dto import Pipeline, VacancyRecord

__all__ = [
    "DbStatus",
    "PreviewContext",
    "PreviewItem",
    "SearchPreview",
    "build_preview",
    "db_status_of",
    "preview_document",
    "render_text_preview",
    "salary_text",
]

# The one-word per-vacancy DB state shown in every output format. Derived with
# first-match-wins priority (see :func:`db_status_of`); the words are
# self-explanatory per the CLI readability rule.
DbStatus = Literal[
    "new",
    "deleted",
    "applied",
    "pending",
    "skipped",
    "error",
    "manual-skip",
    "lettered",
    "scored",
    "fetched",
]


def db_status_of(record: VacancyRecord | None) -> DbStatus:  # noqa: PLR0911 - one early return per vocabulary row is the point
    """Derive the one-word DB state for a listed vacancy (or ``None`` = not stored).

    Most decisive state wins: a vacancy that was scored and then applied reads
    ``applied``; a soft-deleted one reads ``deleted`` no matter what artifacts
    it carries (fetch silently skips soft-deleted rows, so ``new`` would lie).
    """
    if record is None:
        return "new"
    if record.soft_deleted_at is not None:
        return "deleted"
    if record.apply_status is not None:
        return record.apply_status  # the closed ApplyStatus set maps 1:1 onto the words
    if record.manual_skip:
        return "manual-skip"
    # Truthiness on purpose: an emptied cover letter ("" via vacancies edit) is
    # no letter. ``score`` keeps is-not-None — it is a number on the 1..5 scale.
    if record.cover_letter:
        return "lettered"
    if record.score is not None:
        return "scored"
    return "fetched"


def salary_text(salary: Salary | None) -> str:
    """Render a contract salary for the text table (``—`` when absent/empty)."""
    if salary is None:
        return "—"
    bounds: str
    match (salary.from_, salary.to):
        case (None, None):
            bounds = ""
        case (from_, None):
            bounds = f"{from_}+"
        case (None, to):
            bounds = f"≤{to}"
        case (from_, to):
            bounds = f"{from_}-{to}"
    text = " ".join(part for part in (bounds, salary.currency or "") if part)
    return text if text else "—"


@dataclass(frozen=True, slots=True)
class PreviewItem:
    """One search result joined with its stored DB state."""

    external_id: str
    title: str
    url: str
    company: str | None
    salary: Salary | None
    area: str | None
    published_at: str | None
    snippet_requirement: str | None
    snippet_responsibility: str | None
    db_status: DbStatus


@dataclass(frozen=True, slots=True)
class SearchPreview:
    """The full preview document: header facts + joined items."""

    pipeline_label: str  # "name (#id)"
    query: str  # the effective (possibly overridden) query
    query_source: str  # "search N" | "--query"
    params_source: str  # "pipeline filters (no override)" | file path | "stdin" | "inline"
    found: int | None
    board_cap: int | None  # the service's searchable-items cap (for the found note)
    pages_scanned: int
    exhausted: bool
    ui_url: str | None
    items: tuple[PreviewItem, ...]


@dataclass(frozen=True, slots=True)
class PreviewContext:
    """Header facts of one preview run (everything the join needs besides rows)."""

    pipeline: Pipeline
    query: str  # the effective (possibly overridden) query
    query_source: str  # "search N" | "--query"
    params_source: str  # "pipeline filters (no override)" | file path | "stdin" | "inline"
    board_cap: int | None  # the service's searchable-items cap (for the found note)


def build_preview(
    listing: SearchListing,
    context: PreviewContext,
    states_by_external_id: dict[str, VacancyRecord],  # lint-ignore[raw-dict]: join map over storage rows
) -> SearchPreview:
    """Join a listing against the stored rows into a :class:`SearchPreview`."""
    items = tuple(
        PreviewItem(
            external_id=item.external_id,
            title=item.title,
            url=item.url,
            company=item.company,
            salary=item.salary,
            area=item.area,
            published_at=item.published_at,
            snippet_requirement=item.snippet_requirement,
            snippet_responsibility=item.snippet_responsibility,
            db_status=db_status_of(states_by_external_id.get(item.external_id)),
        )
        for item in listing.items
    )
    return SearchPreview(
        pipeline_label=f"{context.pipeline.name} (#{context.pipeline.id})",
        query=context.query,
        query_source=context.query_source,
        params_source=context.params_source,
        found=listing.found,
        board_cap=context.board_cap,
        pages_scanned=listing.pages_scanned,
        exhausted=listing.exhausted,
        ui_url=listing.ui_url,
        items=items,
    )


def preview_document(
    preview: SearchPreview,
) -> dict[str, object]:  # lint-ignore[raw-dict]: JSON/YAML doc  # lint-ignore[restricted-object]: JSON/YAML doc
    """The JSON/YAML document for one preview (serialization artifact only)."""
    return {
        "query": preview.query,
        "query_source": preview.query_source,
        "params_source": preview.params_source,
        "found": preview.found,
        "pages_scanned": preview.pages_scanned,
        "exhausted": preview.exhausted,
        "ui_url": preview.ui_url,
        "items": [
            {
                "external_id": item.external_id,
                "title": item.title,
                "url": item.url,
                "company": item.company,
                "salary": None
                if item.salary is None
                else {
                    "from": item.salary.from_,
                    "to": item.salary.to,
                    "currency": item.salary.currency,
                    "gross": item.salary.gross,
                },
                "area": item.area,
                "published_at": item.published_at,
                "snippet_requirement": item.snippet_requirement,
                "snippet_responsibility": item.snippet_responsibility,
                "db_status": item.db_status,
            }
            for item in preview.items
        ],
    }


_PUBLISHED_DATE_LEN = 10  # ISO datetime prefix kept for the table's published column


def render_text_preview(preview: SearchPreview) -> str:
    """Render the human-readable text form: header + title-first table + hints."""
    query_note = f"  ({preview.query_source})"
    lines: list[str] = [
        f"pipeline: {preview.pipeline_label}",
        f"query:    {preview.query}{query_note}",
        f"params:   {preview.params_source}",
    ]
    found = "unknown" if preview.found is None else str(preview.found)
    cap_note = (
        f" (board cap: {preview.board_cap})"
        if preview.found is not None and preview.board_cap is not None and preview.found >= preview.board_cap
        else ""
    )
    exhausted = "yes" if preview.exhausted else "no"
    lines.append(f"found:    {found}{cap_note}   pages scanned: {preview.pages_scanned}   exhausted: {exhausted}")
    if preview.ui_url is not None:
        lines.append(f"web:      {preview.ui_url}")
    if not preview.items:
        lines.append("")
        lines.append("No vacancies matched. Loosen the query or filters.")
        return "\n".join(lines)

    headers = ("#", "db", "published", "title", "company", "salary", "area")
    rows = [
        (
            str(position),
            item.db_status,
            item.published_at[:_PUBLISHED_DATE_LEN]
            if item.published_at is not None and len(item.published_at) >= _PUBLISHED_DATE_LEN
            else "—",
            item.title,
            item.company or "—",
            salary_text(item.salary),
            item.area or "—",
        )
        for position, item in enumerate(preview.items, start=1)
    ]
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines.append("")
    lines.append("  " + "  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)))
    lines.extend("  " + "  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)) for row in rows)
    noun = "vacancy" if len(rows) == 1 else "vacancies"
    lines.append("")
    lines.append(
        f"{len(rows)} {noun}, {preview.pages_scanned} page(s) scanned. "
        "Iterate with --query/--params; deeper window: --first-page/--take-pages/--page-size"
    )
    return "\n".join(lines)
