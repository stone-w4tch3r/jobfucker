"""HH vacancy search, native paging, and full-detail enrichment."""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Final

import httpx
from pydantic import ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    AuthError,
    BadRequestError,
    ClientError,
    ConfigurationError,
    NotFoundError,
    ProtocolError,
    Salary,
    SearchListing,
    SearchSlice,
    ServiceVacancyId,
    Vacancy,
    VacancyShort,
)
from jobfucker.clients.hh.captcha import CaptchaCoordinator, challenge_error, classify_challenge
from jobfucker.clients.hh.config import HHSearchFilters, HHSearchMode, HHServiceConfig
from jobfucker.clients.hh.models import (
    HHErrorEnvelope,
    VacancyDetailResponse,
    VacancySearchItem,
    VacancySearchResponse,
)
from jobfucker.clients.hh.transport import FormFields, HHTransport
from jobfucker.clients.paging import FetchedListingPage, FetchedPage, scan_listing, scan_slice

_HTTP_OK: Final = 200
_HTTP_BAD_REQUEST: Final = 400
_HTTP_UNAUTHORIZED: Final = frozenset({401, 403})
_HTTP_NOT_FOUND: Final = 404
_MAX_PER_PAGE: Final = 100
_BLOCK_ELEMENTS: Final = frozenset(
    {
        "br",
        "dd",
        "div",
        "dt",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "tr",
    }
)


class DescriptionParser(HTMLParser):
    """Convert trusted-as-text HH description HTML into normalized plaintext."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag.casefold() in _BLOCK_ELEMENTS:
            self.fragments.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in _BLOCK_ELEMENTS:
            self.fragments.append("\n")

    def handle_data(self, data: str) -> None:
        self.fragments.append(data)

    def text(self) -> str:
        """Collapse inline whitespace while retaining meaningful block boundaries."""
        lines = (" ".join(line.split()) for line in "".join(self.fragments).splitlines())
        return "\n".join(line for line in lines if line)


class SearchService:
    """Fetch a position slice of HH listings, enriching only the kept items."""

    def __init__(self, transport: HHTransport, captcha: CaptchaCoordinator, section: HHServiceConfig) -> None:
        self._transport = transport
        self._captcha = captcha
        self._section = section

    async def search(  # noqa: PLR0913 - contract slice params + optional exclusion
        self,
        access_token: str,
        query: str,
        filters: HHSearchFilters,
        *,
        offset: int,
        limit: int,
        page_size: int,
        exclude: frozenset[ServiceVacancyId] = frozenset(),
    ) -> Result[SearchSlice, ClientError]:
        """Return one slice of fully enriched contract vacancies.

        ``query``/``filters`` are one search entry's prebuilt pair (the client
        resolves the pool entry by index before calling). The shared driver
        (:func:`jobfucker.clients.paging.scan_slice`) walks only the native HH
        pages overlapping ``[offset, offset + limit)``; this service's page
        callback fetches the native listing page (one GET, same decode/captcha
        classification as before) and enriches **only** the items whose global
        listing position is inside the page's keep range — out-of-slice items
        are never detail-fetched. Items whose ``external_id`` is in ``exclude``
        (already stored for the pipeline) are skipped before the detail ``GET``
        too, so an insert-only re-fetch does zero detail work.
        """

        async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedPage, ClientError]:
            params_result = _search_params(self._section, filters, query=query, page=page, per_page=page_size)
            if params_result.is_err:
                return Err(params_result.unwrap_err())
            path = _search_path(self._section)
            response_result = await self._get_api(path, access_token, params=params_result.unwrap())
            if response_result.is_err:
                return Err(response_result.unwrap_err())
            page_result = _decode_search_page(response_result.unwrap(), expected_page=page, expected_per_page=page_size)
            if page_result.is_err:
                return Err(page_result.unwrap_err())

            search_page = page_result.unwrap()
            kept: list[Vacancy] = []
            for index, item in enumerate(search_page.items):
                position = page * page_size + index
                if not keep_start <= position < keep_end:
                    continue  # outside the requested slice: never enrich it
                if ServiceVacancyId(item.id) in exclude:
                    continue  # already stored: skip the detail GET entirely
                detail_result = await self._detail(access_token, item.id)
                if detail_result.is_err:
                    return Err(detail_result.unwrap_err())
                kept.append(_to_vacancy(detail_result.unwrap()))
            return Ok(FetchedPage(kept=tuple(kept), listed=len(search_page.items)))

        return await scan_slice(fetch_page, offset=offset, limit=limit, page_size=page_size, exclude=exclude)

    async def list_vacancies(  # noqa: PLR0913 - explicit (transport, filters) seams, contract-shaped
        self,
        access_token: str,
        query: str,
        filters: HHSearchFilters,
        *,
        offset: int,
        limit: int,
        page_size: int,
    ) -> Result[SearchListing, ClientError]:
        """Return one window of the HH listing as short items — no detail enrichment.

        The listing-only twin of :meth:`search` for the preview path: ``query``/
        ``filters`` are one search entry's prebuilt pair; the shared driver
        (:func:`jobfucker.clients.paging.scan_listing`) walks the same native
        pages overlapping ``[offset, offset + limit)`` with the same param
        encoding and captcha-classified transport, but the page callback decodes
        the listing-level short fields straight off the search page — there is
        **no** ``/vacancies/{id}`` detail request and no ``exclude`` (a preview
        wants the whole listing). Envelope metadata (``found``,
        ``alternate_url``) rides along as the board-reported total and UI link.
        """

        async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedListingPage, ClientError]:
            params_result = _search_params(self._section, filters, query=query, page=page, per_page=page_size)
            if params_result.is_err:
                return Err(params_result.unwrap_err())
            path = _search_path(self._section)
            response_result = await self._get_api(path, access_token, params=params_result.unwrap())
            if response_result.is_err:
                return Err(response_result.unwrap_err())
            page_result = _decode_search_page(response_result.unwrap(), expected_page=page, expected_per_page=page_size)
            if page_result.is_err:
                return Err(page_result.unwrap_err())

            search_page = page_result.unwrap()
            # Cap at the page boundary: the envelope echoes per_page (validated),
            # but a misbehaving page with extra items must not double-count
            # positions overlapping the next keep range (scan_slice parity).
            window_start = page * page_size
            window_end = min(window_start + page_size, window_start + len(search_page.items))
            kept_start = max(keep_start, window_start) - window_start
            kept_end = min(keep_end, window_end) - window_start
            return Ok(
                FetchedListingPage(
                    kept=tuple(
                        _to_vacancy_short(item) for item in search_page.items[max(kept_start, 0) : max(kept_end, 0)]
                    ),
                    listed=len(search_page.items),
                    found=search_page.found,
                    ui_url=search_page.alternate_url,
                )
            )

        return await scan_listing(fetch_page, offset=offset, limit=limit, page_size=page_size)

    async def _detail(self, access_token: str, vacancy_id: str) -> Result[VacancyDetailResponse, ClientError]:
        response_result = await self._get_api(f"/vacancies/{vacancy_id}", access_token, params=())
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        response = response_result.unwrap()
        status_error = _status_error(response, operation="vacancy detail")
        if status_error is not None:
            return Err(status_error)
        try:
            detail = VacancyDetailResponse.model_validate_json(response.content)
        except ValidationError:
            return Err(ProtocolError(message="Malformed HH vacancy detail response", status=response.status_code))
        if detail.id != vacancy_id:
            return Err(ProtocolError(message="HH vacancy detail id does not match the requested vacancy"))
        return Ok(detail)

    async def _get_api(
        self,
        path: str,
        access_token: str,
        *,
        params: FormFields,
        allow_captcha_recovery: bool = True,
    ) -> Result[httpx.Response, ClientError]:
        """Classify API CAPTCHA globally, solve once, and replay one safe GET."""
        response_result = await self._transport.get_api(path, access_token, params=params)
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        response = response_result.unwrap()
        challenge = classify_challenge(response, context="api")
        if challenge is None:
            return Ok(response)
        if not allow_captcha_recovery:
            return Err(challenge_error(challenge))
        solved = await self._captcha.solve(challenge)
        if solved.is_err:
            return Err(solved.unwrap_err())
        return await self._get_api(
            path,
            access_token,
            params=params,
            allow_captcha_recovery=False,
        )


def _search_path(section: HHServiceConfig) -> str:
    """Select the configured endpoint without inferring mode from the query."""
    if section.search_mode is HHSearchMode.CATALOG:
        return "/vacancies"
    return f"/resumes/{section.resume_id}/similar_vacancies"


def _search_params(
    section: HHServiceConfig,
    filters: HHSearchFilters,
    *,
    query: str,
    page: int,
    per_page: int,
) -> Result[FormFields, ClientError]:
    """Encode one entry's full filter surface into repeated HH query params.

    Booleans serialize as lowercase ``"true"`` and are emitted only when set
    (absence means false); list filters repeat one query param per value
    (comma-joined values are rejected upstream); defaults and ``None`` fields
    are omitted. ``filters`` is the entry's prebuilt wire model (the caller
    resolves the pool entry by index). The two context checks here need the
    section/mode, which the filter model cannot see: ``search_field`` is inert
    without a query, and ``saved_search_id`` is rejected on resume-similar
    search — the catalog endpoint silently ignores a nonexistent/not-owned ID
    while resume-scoped search returns ``400 bad_argument``
    (docs/hh/api/search.md).
    """
    if page < 0:
        return Err(BadRequestError(message="HH search page must be >= 0"))
    if not 1 <= per_page <= _MAX_PER_PAGE:
        return Err(BadRequestError(message="HH search per_page must be between 1 and 100"))
    if filters.search_field and query == "":
        return Err(ConfigurationError(message="HH search_field filters require a non-empty query"))
    if section.search_mode is HHSearchMode.RESUME_SIMILAR and filters.saved_search_id is not None:
        return Err(
            ConfigurationError(
                message=(
                    "HH resume-similar search rejects saved_search_id: the catalog endpoint "
                    "ignores a nonexistent/not-owned ID, resume-scoped search returns 400 bad_argument"
                )
            )
        )

    params: FormFields = (("text", query), ("page", str(page)), ("per_page", str(per_page)))
    params = _encode_core(params, filters)
    params = _encode_work_arrangement(params, filters)
    params = _encode_salary_and_dates(params, filters)
    params = _encode_geography(params, filters)
    params = _encode_employers_and_labels(params, filters)
    return Ok(params)


def _encode_core(params: FormFields, filters: HHSearchFilters) -> FormFields:
    """Encode text-adjacent flags, search fields, and sorting (wiki: core query and sorting)."""
    encoded = params
    if filters.no_magic:
        encoded += (("no_magic", "true"),)
    encoded += tuple(("search_field", field) for field in filters.search_field)
    if filters.order_by != "relevance":
        encoded += (("order_by", filters.order_by),)
    if filters.sort_point_lat is not None:
        encoded += (("sort_point_lat", str(filters.sort_point_lat)),)
    if filters.sort_point_lng is not None:
        encoded += (("sort_point_lng", str(filters.sort_point_lng)),)
    if filters.clusters:
        encoded += (("clusters", "true"),)
    if filters.describe_arguments:
        encoded += (("describe_arguments", "true"),)
    if filters.responses_count_enabled:
        encoded += (("responses_count_enabled", "true"),)
    return encoded


def _encode_work_arrangement(params: FormFields, filters: HHSearchFilters) -> FormFields:
    """Encode schedule, format, employment, and hours families (wiki: work arrangement)."""
    encoded = params
    encoded += tuple(("schedule", value) for value in filters.schedule)
    encoded += tuple(("work_format", value) for value in filters.work_format)
    encoded += tuple(("employment", value) for value in filters.employment)
    encoded += tuple(("employment_form", value) for value in filters.employment_form)
    if filters.experience is not None:
        encoded += (("experience", filters.experience),)
    if filters.education is not None:
        encoded += (("education", filters.education),)
    encoded += tuple(("part_time", value) for value in filters.part_time)
    encoded += tuple(("work_schedule_by_days", value) for value in filters.work_schedule_by_days)
    encoded += tuple(("working_hours", value) for value in filters.working_hours)
    if filters.accept_temporary:
        encoded += (("accept_temporary", "true"),)
    return encoded


def _encode_salary_and_dates(params: FormFields, filters: HHSearchFilters) -> FormFields:
    """Encode the salary fork and publication-time families (wiki: salary, publication time)."""
    encoded = params
    if filters.salary is not None:
        encoded += (("salary", str(filters.salary)),)
    if filters.currency is not None:
        encoded += (("currency", filters.currency),)
    if filters.only_with_salary:
        encoded += (("only_with_salary", "true"),)
    encoded += tuple(("salary_frequency", value) for value in filters.salary_frequency)
    encoded += tuple(("salary_mode", value) for value in filters.salary_mode)
    if filters.period is not None:
        encoded += (("period", str(filters.period)),)
    if filters.date_from is not None:
        encoded += (("date_from", filters.date_from.isoformat()),)
    if filters.date_to is not None:
        encoded += (("date_to", filters.date_to.isoformat()),)
    return encoded


def _encode_geography(params: FormFields, filters: HHSearchFilters) -> FormFields:
    """Encode area/metro/district and the four-corner bbox (wiki: geography)."""
    encoded = params
    encoded += tuple(("area", str(value)) for value in filters.area)
    encoded += tuple(("metro", value) for value in filters.metro)
    encoded += tuple(("district", str(value)) for value in filters.district)
    if filters.bottom_left_lat is not None:
        encoded += (("bottom_left_lat", str(filters.bottom_left_lat)),)
    if filters.bottom_left_lng is not None:
        encoded += (("bottom_left_lng", str(filters.bottom_left_lng)),)
    if filters.top_right_lat is not None:
        encoded += (("top_right_lat", str(filters.top_right_lat)),)
    if filters.top_right_lng is not None:
        encoded += (("top_right_lng", str(filters.top_right_lng)),)
    return encoded


def _encode_employers_and_labels(params: FormFields, filters: HHSearchFilters) -> FormFields:
    """Encode employer, profession, exclusion, and label families (wiki: employer/profession/labels)."""
    encoded = params
    encoded += tuple(("professional_role", str(value)) for value in filters.professional_role)
    encoded += tuple(("industry", str(value)) for value in filters.industry)
    encoded += tuple(("employer_id", str(value)) for value in filters.employer_id)
    encoded += tuple(("excluded_employer_id", str(value)) for value in filters.excluded_employer_id)
    if filters.excluded_text is not None:
        encoded += (("excluded_text", filters.excluded_text),)
    encoded += tuple(("label", value) for value in filters.label)
    if filters.saved_search_id is not None:
        encoded += (("saved_search_id", filters.saved_search_id),)
    return encoded


def _decode_search_page(
    response: httpx.Response,
    *,
    expected_page: int,
    expected_per_page: int,
) -> Result[VacancySearchResponse, ClientError]:
    """Decode one successful page and reject 200-level HH error envelopes."""
    status_error = _status_error(response, operation="vacancy search")
    if status_error is not None:
        return Err(status_error)
    try:
        error_envelope = HHErrorEnvelope.model_validate_json(response.content)
    except ValidationError:
        error_envelope = None
    if error_envelope is not None and error_envelope.errors:
        return Err(BadRequestError(message="HH vacancy search rejected the request"))
    try:
        page = VacancySearchResponse.model_validate_json(response.content)
    except ValidationError:
        return Err(ProtocolError(message="Malformed HH vacancy search response", status=response.status_code))
    if page.page != expected_page or page.per_page != expected_per_page:
        return Err(ProtocolError(message="HH vacancy search returned unexpected paging metadata"))
    return Ok(page)


def _status_error(response: httpx.Response, *, operation: str) -> ClientError | None:
    """Map endpoint status without exposing an untrusted upstream response body."""
    if response.status_code == _HTTP_OK:
        return None
    if response.status_code in _HTTP_UNAUTHORIZED:
        return AuthError(message=f"HH rejected authorization during {operation}")
    if response.status_code == _HTTP_BAD_REQUEST:
        return BadRequestError(message=f"HH rejected {operation}")
    if response.status_code == _HTTP_NOT_FOUND:
        return NotFoundError(message=f"HH {operation} resource was not found")
    return ProtocolError(message=f"Unexpected HH {operation} status", status=response.status_code)


def _to_vacancy_short(item: VacancySearchItem) -> VacancyShort:
    """Map a validated HH search item into the board-neutral short contract.

    Raw passthrough for snippets (``<highlighttext>`` markup allowed — they are
    preview fragments, documented as such). ``name``/``alternate_url`` are
    documented key fields and always present in practice; a degenerate item
    without them maps to an empty title/url rather than failing the whole
    listing decode (the enrichment path only ever reads ``id``).
    """
    return VacancyShort(
        external_id=ServiceVacancyId(item.id),
        title=item.name or "",
        url=item.alternate_url or "",
        company=item.employer.name if item.employer is not None else None,
        salary=(
            Salary(
                from_=item.salary.from_,
                to=item.salary.to,
                currency=item.salary.currency,
                gross=item.salary.gross,
            )
            if item.salary is not None
            else None
        ),
        area=item.area.name if item.area is not None else None,
        published_at=item.published_at,
        snippet_requirement=item.snippet.requirement if item.snippet is not None else None,
        snippet_responsibility=item.snippet.responsibility if item.snippet is not None else None,
    )


def _to_vacancy(detail: VacancyDetailResponse) -> Vacancy:
    """Map a validated HH detail response into the board-neutral contract."""
    parser = DescriptionParser()
    parser.feed(detail.description)
    parser.close()
    salary = (
        Salary(
            from_=detail.salary.from_,
            to=detail.salary.to,
            currency=detail.salary.currency,
            gross=detail.salary.gross,
        )
        if detail.salary is not None
        else None
    )
    return Vacancy(
        external_id=ServiceVacancyId(detail.id),
        title=detail.name,
        url=detail.alternate_url,
        company=detail.employer.name if detail.employer is not None else None,
        description=parser.text(),
        key_skills=tuple(skill.name for skill in detail.key_skills),
        salary=salary,
        has_hh_test=detail.has_test,
    )
