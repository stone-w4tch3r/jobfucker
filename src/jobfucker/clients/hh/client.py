"""Board-neutral ``Client`` implementation for HH.ru."""

from __future__ import annotations

import httpx
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    ApplyResult,
    Client,
    ClientDeps,
    ClientError,
    ConfigurationError,
    ResumeInfo,
    SearchListing,
    SearchSlice,
    ServiceConfigSection,
    ServiceInfo,
    ServiceVacancyId,
)
from jobfucker.clients.hh.applications import ApplicationService, ApplyDelay
from jobfucker.clients.hh.auth import AuthCoordinator
from jobfucker.clients.hh.browser import (
    BROWSER_UNAVAILABLE_PREFIX,
    BrowserCaptchaSolver,
    BrowserDriver,
    PatchrightDriver,
)
from jobfucker.clients.hh.captcha import CaptchaCoordinator
from jobfucker.clients.hh.config import HHSearchFilters, HHServiceConfig
from jobfucker.clients.hh.resumes import ResumeService
from jobfucker.clients.hh.search import SearchService
from jobfucker.clients.hh.tests import HhTestService
from jobfucker.clients.hh.transport import HHTransport
from jobfucker.hh_tests.contract import HhTestCapable, HhTestProblem, HhTestSolution

_HH_CONSERVATIVE_DAILY_CAP = 200
# HH caps searchable results at 2000 items (maxSearchResult=2000); an offset
# beyond it is a board-side 400. The fetch stage validates its window against
# this cap before any request (docs/hh/api/search.md).
_HH_MAX_SEARCH_ITEMS = 2000


class HHClient(Client, HhTestCapable):
    """HTTP-only HH client with authentication preflight on every action.

    Also implements :class:`HhTestCapable` — the HH-only screening-test
    capability the core apply stage gates on via ``isinstance``. Mock and
    future boards do not implement it, so the gate excludes them by
    construction.
    """

    service = "hh"
    service_info = ServiceInfo(
        service="hh",
        per_auth_daily_cap=_HH_CONSERVATIVE_DAILY_CAP,
        max_search_items=_HH_MAX_SEARCH_ITEMS,
    )

    def __init__(
        self,
        deps: ClientDeps,
        section: ServiceConfigSection,
        *,
        http_transport: httpx.AsyncBaseTransport | None = None,
        browser_driver: BrowserDriver | None = None,
        apply_delay: ApplyDelay | None = None,
    ) -> None:
        if not isinstance(section, HHServiceConfig):
            raise TypeError(f"HHClient expects HHServiceConfig, got {type(section).__name__}")
        if not deps.credentials.login.strip() or not deps.credentials.password:
            raise ValueError("HHClient requires resolved non-empty login credentials")
        self._deps = deps
        self._section = section
        # Prebuild one (query, wire filters) pair per pool entry: the contract
        # methods address an entry by its stable zero-based search_index and
        # never see the board filter.
        self._entries: tuple[tuple[str, HHSearchFilters], ...] = tuple(
            (entry.query, entry.filter.to_search_filters()) for entry in section.searches
        )
        self._transport = HHTransport(transport=http_transport)
        standalone_solver = BrowserCaptchaSolver(
            browser_driver if browser_driver is not None else PatchrightDriver(reporter=deps.reporter),
            deps.captcha_handler,
            max_attempts=section.captcha_max_attempts,
        )
        self._standalone_solver = standalone_solver
        captcha = CaptchaCoordinator(
            self._transport,
            deps.captcha_handler,
            max_attempts=section.captcha_max_attempts,
            standalone_solver=standalone_solver,
            reporter=deps.reporter,
        )
        self._auth = AuthCoordinator(deps, self._transport, captcha)
        self._search = SearchService(self._transport, captcha, section)
        self._resumes = ResumeService(self._transport, captcha)
        self._applications = ApplicationService(self._transport, captcha, delay=apply_delay)
        self._tests = HhTestService(self._transport)
        # Resume ids already proven owned+published on this client instance;
        # the walk over /resumes/mine runs once per id, not once per vacancy.
        self._validated_resume_ids: set[str] = set()

    async def authorize(self) -> Result[None, ClientError]:
        """Ensure current persisted or newly-created HH authorization is healthy."""
        return await self._ensure_authorized()

    async def _ensure_authorized(self) -> Result[None, ClientError]:
        """Shared board-action preflight: browser engine (once) + healthy auth.

        Nearly every board-facing action can hit a standalone CAPTCHA, so the
        first action installs the engine up front (client startup). A failed
        preflight fails the action fast — without the engine the run would die
        mid-batch on the first standalone CAPTCHA anyway.
        """
        engine = await self._standalone_solver.ensure_engine()
        if engine.is_err:
            return Err(ConfigurationError(message=f"{BROWSER_UNAVAILABLE_PREFIX} ({engine.unwrap_err()})"))
        return await self._auth.ensure_authorized()

    async def search_vacancies(
        self,
        search_index: int,
        *,
        offset: int = 0,
        limit: int = 100,
        page_size: int = 100,
        exclude: frozenset[ServiceVacancyId] = frozenset(),
    ) -> Result[SearchSlice, ClientError]:
        """Healthcheck authorization once, then fetch one enriched listing slice.

        ``search_index`` selects one prebuilt ``(query, filters)`` pool entry;
        a stale/foreign index is a :class:`ConfigurationError`. The auth
        preflight runs once per slice (not per native page); the walk over the
        native pages overlapping ``[offset, offset + limit)`` and the
        keep-range-only enrichment happen in :class:`SearchService` through the
        shared paging driver. Stored ids in ``exclude`` are skipped before their
        detail ``GET`` (insert-only re-fetches do no detail work).
        """
        entry_result = self._entry(search_index)
        if entry_result.is_err:
            return Err(entry_result.unwrap_err())
        query, filters = entry_result.unwrap()
        authorized = await self._ensure_authorized()
        if authorized.is_err:
            return Err(authorized.unwrap_err())
        return await self._search.search(
            self._auth.authorized_access_token,
            query,
            filters,
            offset=offset,
            limit=limit,
            page_size=page_size,
            exclude=exclude,
        )

    async def list_vacancies(
        self,
        search_index: int,
        *,
        offset: int = 0,
        limit: int = 100,
        page_size: int = 100,
    ) -> Result[SearchListing, ClientError]:
        """Healthcheck authorization once, then return one listing-only window.

        The preview counterpart of :meth:`search_vacancies`: ``search_index``
        selects one prebuilt pool entry; the same native pages are walked with
        the same filter encoding and captcha handling, but only the listing-level
        short fields are decoded — there is **no** ``/vacancies/{id}`` detail
        request and nothing is persisted. Unlike :meth:`search_vacancies` a
        failed page is a plain ``Err`` (no partial result: a listing has no
        persistence to protect).
        """
        entry_result = self._entry(search_index)
        if entry_result.is_err:
            return Err(entry_result.unwrap_err())
        query, filters = entry_result.unwrap()
        authorized = await self._ensure_authorized()
        if authorized.is_err:
            return Err(authorized.unwrap_err())
        return await self._search.list_vacancies(
            self._auth.authorized_access_token,
            query,
            filters,
            offset=offset,
            limit=limit,
            page_size=page_size,
        )

    def _entry(self, search_index: int) -> Result[tuple[str, HHSearchFilters], ClientError]:
        """Resolve a pool index onto its prebuilt ``(query, filters)`` pair."""
        if not 0 <= search_index < len(self._entries):
            return Err(
                ConfigurationError(
                    message=(
                        f"HH search index {search_index} is outside the pipeline's "
                        f"search pool of {len(self._entries)} entries"
                    )
                )
            )
        return Ok(self._entries[search_index])

    async def get_resumes(self) -> Result[list[ResumeInfo], ClientError]:
        """Healthcheck authorization, then list the account's owned resumes."""
        authorized = await self._ensure_authorized()
        if authorized.is_err:
            return Err(authorized.unwrap_err())
        listed = await self._resumes.list_resumes(self._auth.authorized_access_token)
        if listed.is_err:
            return Err(listed.unwrap_err())
        return Ok(
            [ResumeInfo(resume_id=item.id, title=item.title, updated_at=item.updated_at) for item in listed.unwrap()]
        )

    async def apply_to_vacancy(
        self,
        *,
        resume_id: str,
        vacancy_id: ServiceVacancyId,
        message: str | None = None,
    ) -> Result[ApplyResult, ClientError]:
        """Healthcheck authorization, validate the resume once, then apply.

        The configured resume must be owned and published; a failed validation
        is a :class:`ConfigurationError` the core treats as a pipeline stop —
        it is memoized per client so a batch validates once, not per vacancy.
        """
        authorized = await self._ensure_authorized()
        if authorized.is_err:
            return Err(authorized.unwrap_err())
        if resume_id not in self._validated_resume_ids:
            validated = await self._resumes.validate_resume(self._auth.authorized_access_token, resume_id)
            if validated.is_err:
                return Err(validated.unwrap_err())
            self._validated_resume_ids.add(resume_id)
        return await self._applications.apply(
            access_token=self._auth.authorized_access_token,
            resume_id=resume_id,
            vacancy_id=vacancy_id,
            message=message,
        )

    async def apply_to_vacancy_with_test(
        self,
        *,
        resume_id: str,
        vacancy_id: ServiceVacancyId,
        message: str | None,
        solution: HhTestSolution,
    ) -> Result[ApplyResult, ClientError]:
        """Healthcheck authorization, validate the resume once, then web-apply with answers.

        The website multipart flow (fresh blob at submit, task-id re-validation,
        tests.md §2). When the fresh page shows the test was removed while the
        vacancy stays open, ``HhTestService`` signals it by returning ``None``
        and this method falls back ONCE to the standard application flow.
        """
        authorized = await self._ensure_authorized()
        if authorized.is_err:
            return Err(authorized.unwrap_err())
        if resume_id not in self._validated_resume_ids:
            validated = await self._resumes.validate_resume(self._auth.authorized_access_token, resume_id)
            if validated.is_err:
                return Err(validated.unwrap_err())
            self._validated_resume_ids.add(resume_id)
        outcome = await self._tests.apply_with_test(
            resume_id=resume_id,
            vacancy_id=vacancy_id,
            message=message,
            solution=solution,
        )
        if outcome.is_err:
            return Err(outcome.unwrap_err())
        result = outcome.unwrap()
        if result is not None:
            return Ok(result)
        # Test removed since solve while the vacancy is still applyable: route
        # once through the standard flow (its preflight re-checks archived/closed).
        return await self.apply_to_vacancy(resume_id=resume_id, vacancy_id=vacancy_id, message=message)

    async def get_vacancy_test(self, vacancy_id: ServiceVacancyId) -> Result[HhTestProblem | None, ClientError]:
        """Healthcheck authorization, then fetch the vacancy's web screening test."""
        authorized = await self._ensure_authorized()
        if authorized.is_err:
            return Err(authorized.unwrap_err())
        return await self._tests.get_vacancy_test(vacancy_id)

    async def aclose(self) -> None:
        """Release the owned HTTP transport; safe to call repeatedly."""
        await self._transport.aclose()
