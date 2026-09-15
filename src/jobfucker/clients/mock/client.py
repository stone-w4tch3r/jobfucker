"""First-party Mock client — no-network client registered under service ``"mock"``.

Lets the full product (CLI + GUI + pipeline) run and be manually tested end-to-end
with **no live board**. It is a real,
useful client: it implements every ``Client`` method through the same
constructor contract (:class:`ClientDeps`) a real client uses, and it
**self-configures from its own ``service.mock`` section** — reading its
per-entry search pools (``section.searches[].vacancies``) and its programmable
manual-testing behavior (``section.behavior``) from the section, never from core
and never from a separate fixture file. The four apply
outcomes (``applied``/``skipped``/``error``/``limit_exceeded``), a failing
``authorize``, and a scripted offline **captcha challenge** (``behavior.captcha``,
routed through the injected :data:`CaptchaHandler` so the CLI's sixel/kitty
rendering + human solving can be exercised with no live board) are all driven
via the section's ``behavior`` block.
"""

from __future__ import annotations

from collections.abc import Awaitable
from functools import cache
from pathlib import Path

from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    ApplyError,
    ApplyFailed,
    ApplyResult,
    ApplySkip,
    ApplySkipped,
    ApplySucceeded,
    AuthError,
    CaptchaSolvingError,
    Client,
    ClientDeps,
    ClientError,
    ConfigurationError,
    LimitExceededError,
    ResumeInfo,
    Salary,
    SearchListing,
    SearchSlice,
    ServiceConfigSection,
    ServiceInfo,
    ServiceVacancyId,
    Vacancy,
    VacancyShort,
)
from jobfucker.clients.mock.params import (
    MockSearchEntry,
    MockServiceConfig,
    MockVacancyParams,
)
from jobfucker.clients.paging import FetchedListingPage, FetchedPage, scan_listing, scan_slice
from jobfucker.reporting import EventLevel, RunEvent

# Mock's declared per-auth daily cap, so cap validation (Phase 4) is exercised.
_MOCK_PER_AUTH_DAILY_CAP = 200

# Fixed resume the mock reports from get_resumes().
_MOCK_RESUME = ResumeInfo(
    resume_id="mock-resume-1",
    title="Mock Resume (offline test data)",
    updated_at="2026-08-05T00:00:00Z",
)


@cache
def _load_captcha_image() -> bytes:
    """Load the mock's canned captcha PNG (rendered by the CLI, offline).

    A real captcha image is needed so the terminal/AI captcha handlers have
    something to render/solve; the bytes come from the packaged
    ``resources/mock_client_captcha.png``. The run's vacancies live in
    ``section.vacancies`` (no fixture file), so this PNG is the only canned
    file the mock reads. Cached per-process with :func:`functools.cache` — the
    PNG bytes are immutable and shared across :class:`MockClient` instances, so
    the blocking read happens once.
    """
    return (Path(__file__).parents[2] / "resources" / "mock_client_captcha.png").read_bytes()


def _to_vacancy(payload: MockVacancyParams) -> Vacancy:
    """Map a validated section vacancy to a contract ``Vacancy`` (incl. nested salary)."""
    salary = (
        Salary(
            from_=payload.salary.from_,
            to=payload.salary.to,
            currency=payload.salary.currency,
            gross=payload.salary.gross,
        )
        if payload.salary is not None
        else None
    )
    return Vacancy(
        external_id=ServiceVacancyId(payload.external_id),
        title=payload.title,
        url=payload.url,
        company=payload.company,
        description=payload.description,
        key_skills=tuple(payload.key_skills),
        salary=salary,
    )


def _to_vacancy_short(vacancy: Vacancy) -> VacancyShort:
    """Map a canned ``Vacancy`` onto its short listing form.

    The mock's canned vacancies are enriched by construction, so the short view
    is a strict projection: no area/published-at/snippet (the mock has no
    listing-page snippets) and no UI URL (the mock has no web page — the
    envelope metadata stays ``None``).
    """
    return VacancyShort(
        external_id=vacancy.external_id,
        title=vacancy.title,
        url=vacancy.url,
        company=vacancy.company,
        salary=vacancy.salary,
        area=None,
        published_at=None,
        snippet_requirement=None,
        snippet_responsibility=None,
    )


class MockClient(Client):
    """A scriptable, no-network client implementing the ``Client`` contract.

    Construct exactly like a real client — from :class:`ClientDeps` plus the
    run's ``service.mock`` config section (a :class:`MockServiceConfig`). It
    reads its search filter from ``section.filter``, its canned vacation list
    from ``section.vacancies``, and its programmable behavior from
    ``section.behavior``; when no behavior is configured, the default is
    "everything succeeds".
    """

    service: str = "mock"
    service_info: ServiceInfo = ServiceInfo(
        service="mock",
        per_auth_daily_cap=_MOCK_PER_AUTH_DAILY_CAP,
        max_search_items=None,  # the canned mock listing has no search cap
    )

    def __init__(self, deps: ClientDeps, section: ServiceConfigSection) -> None:
        """Build a mock client from the standard client deps + its own section.

        Args:
            deps: the standard :class:`ClientDeps` (data_dir, interaction,
                captcha handler) every client receives.
            section: the validated ``service.mock`` config section.

        Raises:
            TypeError: when ``section`` is not a :class:`MockServiceConfig`.
        """
        # Narrowing to the concrete section type is an invariant: a client is
        # only ever handed its own board's section. Failing that is a
        # programmer/config error, so it raises (fail-fast).
        if not isinstance(section, MockServiceConfig):
            raise TypeError(f"MockClient expects MockServiceConfig, got {type(section).__name__}")
        self._deps: ClientDeps = deps
        self._section: MockServiceConfig = section
        self._behavior = section.behavior
        # One canned vacancy pool per search entry: the contract methods address
        # an entry by its stable zero-based search_index.
        self._entries: tuple[MockSearchEntry, ...] = tuple(section.searches)
        self._pools: tuple[tuple[Vacancy, ...], ...] = tuple(
            tuple(_to_vacancy(v) for v in entry.vacancies) for entry in self._entries
        )
        self._captcha_image: bytes = _load_captcha_image()

    def _entry_pool(self, search_index: int) -> Result[tuple[Vacancy, ...], ClientError]:
        """Resolve a pool index onto its canned vacancies (fail on a stale index)."""
        if not 0 <= search_index < len(self._pools):
            return Err(
                ConfigurationError(
                    message=(
                        f"Mock search index {search_index} is outside the pipeline's "
                        f"search pool of {len(self._pools)} entries"
                    )
                )
            )
        return Ok(self._pools[search_index])

    async def _report(self, message: str, *, level: EventLevel = "debug") -> None:
        """Publish a board-operation event on the shared sink.

        ``deps.reporter`` is **never** ``None`` (GUI, terminal, or the tests'
        no-op default), so this publishes unconditionally. Board-operation
        detail defaults to ``debug`` (shown only at ``-vv``); failures pass
        ``level="error"`` so they always surface.
        """
        await self._deps.reporter.publish(
            RunEvent(
                stage="client",
                message=message,
                level=level,
            )
        )

    async def _solve_captchas(self, operation: str) -> Result[None, ClientError]:
        """Require solving the mock's captcha ``operation`` times before it proceeds.

        Matches :data:`MockCaptchaConfig.operation`; when it fires, the mock
        routes its canned captcha image through the injected
        :data:`CaptchaHandler` once per ``attempts``. A handler failure (EOF /
        unsolvable) collapses into ``Err(CaptchaSolvingError)`` — the same
        surface as an unsolvable real captcha. No behavior or a non-matching
        ``operation`` is a no-op ``Ok``. The handler is an async, human-interaction
        seam (contract §7.1), so it is awaited directly — the handler's own
        implementation keeps blocking I/O off the loop.
        """
        behavior = self._behavior
        if behavior is None or behavior.captcha is None:
            return Ok(None)
        if behavior.captcha.operation != operation:
            return Ok(None)
        for _ in range(behavior.captcha.attempts):
            solved = await self._deps.captcha_handler(self._captcha_image)
            if solved.is_err:
                return Err(CaptchaSolvingError(message=solved.unwrap_err()))
        return Ok(None)

    async def authorize(self) -> Result[None, ClientError]:
        """Succeed (no-op) unless ``behavior.authorize_error`` is set.

        Returns:
            ``Ok(None)`` by default, ``Err(AuthError)`` when told to fail, or
            ``Err(CaptchaSolvingError)`` when a captcha is scripted on
            ``authorize`` and cannot be solved.
        """
        captcha = await self._solve_captchas("authorize")
        if captcha.is_err:
            await self._report("authorize: blocked by unresolved captcha", level="error")
            return captcha
        if self._behavior is not None and self._behavior.authorize_error is not None:
            await self._report("authorize: failed", level="error")
            return Err(AuthError(message=self._behavior.authorize_error))
        await self._report("authorize: ok")
        return Ok(None)

    async def _fetch_listing_page(
        self,
        canned: tuple[Vacancy, ...],
        page: int,
        keep_start: int,
        keep_end: int,
        *,
        page_size: int,
    ) -> Result[FetchedPage, ClientError]:
        """One native page of a canned listing, trimmed to the keep range.

        The seam the shared driver calls per page (and the seam test doubles
        override to script per-page failures).
        """
        page_start = page * page_size
        window = canned[page_start : page_start + page_size]
        # Trim to the requested global positions: only items inside the keep
        # range are ever "enriched" (returned), matching the contract.
        kept = window[max(0, keep_start - page_start) : max(0, keep_end - page_start)]
        return Ok(FetchedPage(kept=tuple(kept), listed=len(window)))

    async def search_vacancies(
        self,
        search_index: int,
        *,
        offset: int = 0,
        limit: int = 100,
        page_size: int = 100,
        exclude: frozenset[ServiceVacancyId] = frozenset(),
    ) -> Result[SearchSlice, ClientError]:
        """Return a slice of one entry's canned vacancies via the shared driver.

        Args:
            search_index: 0-based position in the pipeline's search pool;
                selects the entry whose canned vacancies are served.
            offset: 0-based listing position of the first wanted item.
            limit: how many items the caller persists (``[offset, offset+limit)``).
            page_size: native page size the walk uses.
            exclude: stored ids to drop from the returned slice (the driver
                filters them centrally; the mock enriches nothing, so no work
                is saved here — the flag exists for contract parity).

        Returns:
            ``Ok`` with the slice of canned vacancies, or ``Err`` when a
            captcha is scripted on ``search`` and cannot be solved (the
            challenge fires **once per slice call**, not per page), the paging
            arguments are invalid, or the index is outside the pool.
        """
        # Pool validation first: a bad index must be a config error even when
        # a captcha is scripted on this operation.
        pool_result = self._entry_pool(search_index)
        if pool_result.is_err:
            return Err(pool_result.unwrap_err())
        canned = pool_result.unwrap()
        captcha = await self._solve_captchas("search")
        if captcha.is_err:
            return Err(captcha.unwrap_err())

        def fetch_page(page: int, keep_start: int, keep_end: int) -> Awaitable[Result[FetchedPage, ClientError]]:
            return self._fetch_listing_page(canned, page, keep_start, keep_end, page_size=page_size)

        return await scan_slice(
            fetch_page,
            offset=offset,
            limit=limit,
            page_size=page_size,
            exclude=exclude,
            reporter=self._deps.reporter,
        )

    async def list_vacancies(
        self,
        search_index: int,
        *,
        offset: int = 0,
        limit: int = 100,
        page_size: int = 100,
    ) -> Result[SearchListing, ClientError]:
        """Return a window of one entry's canned vacancies as short items.

        The listing-only twin of :meth:`search_vacancies` for the preview path:
        same captcha-on-search behavior (once per call), same page geometry
        through the shared driver, but the selected entry's canned vacancies are
        projected onto :class:`VacancyShort` and the envelope metadata is
        board-absent (``found`` = the canned total, ``ui_url`` = ``None`` — the
        mock has no web page). There is no ``exclude`` by contract.
        """
        # Pool validation first: a bad index must be a config error even when
        # a captcha is scripted on this operation.
        pool_result = self._entry_pool(search_index)
        if pool_result.is_err:
            return Err(pool_result.unwrap_err())
        canned = pool_result.unwrap()
        captcha = await self._solve_captchas("search")
        if captcha.is_err:
            return Err(captcha.unwrap_err())
        canned_total = len(canned)

        async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedListingPage, ClientError]:
            result = await self._fetch_listing_page(canned, page, keep_start, keep_end, page_size=page_size)
            if result.is_err:
                return Err(result.unwrap_err())
            fetched = result.unwrap()
            return Ok(
                FetchedListingPage(
                    kept=tuple(_to_vacancy_short(vacancy) for vacancy in fetched.kept),
                    listed=fetched.listed,
                    found=canned_total,
                    ui_url=None,
                )
            )

        return await scan_listing(
            fetch_page,
            offset=offset,
            limit=limit,
            page_size=page_size,
            reporter=self._deps.reporter,
        )

    async def get_resumes(self) -> Result[list[ResumeInfo], ClientError]:
        """Return a fixed mock resume list."""
        return Ok([_MOCK_RESUME])

    async def apply_to_vacancy(
        self,
        *,
        resume_id: str,
        vacancy_id: ServiceVacancyId,
        message: str | None = None,
    ) -> Result[ApplyResult, ClientError]:
        """Apply to a mocked vacancy, honouring the configured behavior.

        Behavior comes from ``section.behavior``: a per-vacancy entry (matched
        by external vacancy id) wins over ``default_apply``; when no behavior or
        no ``default_apply`` is configured, the default is an ``applied`` outcome
        echoing the submitted ``message``.

        Args:
            resume_id: ignored by the mock (accepted, not used).
            vacancy_id: selects the behavior via ``behavior.per_vacancy`` (falls
                back to ``behavior.default_apply``, then to a default "applied").
            message: cover-letter text echoed back on an ``applied`` outcome.

        Returns:
            A classified ``ApplyResult`` or the ``limit_exceeded`` ``Err`` as configured; a scripted
            captcha whose solve fails collapses into
            ``Err(CaptchaSolvingError)``.
        """
        captcha = await self._solve_captchas("apply")
        if captcha.is_err:
            await self._report(f"apply: blocked for vacancy #{vacancy_id} (captcha)", level="error")
            return Err(captcha.unwrap_err())
        del resume_id
        await self._report(f"apply: sending to vacancy #{vacancy_id}")
        entry = self._behavior.default_apply if self._behavior is not None else None
        if self._behavior is not None and vacancy_id in self._behavior.per_vacancy:
            entry = self._behavior.per_vacancy[vacancy_id]
        # No behavior and no default_apply => default "applied", echoing the message.
        result: Result[ApplyResult, ClientError]
        if entry is None:
            result = Ok(ApplySucceeded())
        else:
            match entry.outcome:
                case "applied":
                    result = Ok(ApplySucceeded())
                case "skipped":
                    result = Ok(
                        ApplySkipped(
                            ApplySkip(
                                reason="vacancy_unavailable",
                                text=entry.message or "mock application skipped",
                            )
                        )
                    )
                case "error":
                    result = Ok(ApplyFailed(ApplyError(text=entry.message or "mock apply rejected")))
                case "limit_exceeded":
                    result = Err(LimitExceededError(message=entry.message or "mock per-auth daily cap reached"))
        del message
        if result.is_ok and isinstance(result.unwrap(), ApplySucceeded):
            await self._report(f"apply: ok for vacancy #{vacancy_id}", level="success")
        return result

    async def aclose(self) -> None:
        """Release owned resources; the in-memory mock owns none."""
