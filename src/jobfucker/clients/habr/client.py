"""Board-neutral ``Client`` implementation for Habr Career (``career.habr.com``).

Unlike HH, Habr supports **anonymous reads**: ``type=all`` listings and the
preview path need no session. Only ``type=suitable`` searches and the
identity/resume/apply actions require authorization. The client therefore does
no auth preflight for an ``all`` listing (a preview/fetch must never force a
captcha login) and fails closed with a :class:`ConfigurationError` for a
``suitable`` entry when no credentials are available — before any request.

Habr has no screening tests: this client deliberately does **not** implement
``HhTestCapable``, and ``Vacancy.has_hh_test`` stays ``None``.
"""

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
    ServiceIdentity,
    ServiceInfo,
    ServiceVacancyId,
)
from jobfucker.clients.habr.applications import ApplicationService, ApplyDelay
from jobfucker.clients.habr.auth import AuthCoordinator
from jobfucker.clients.habr.browser import HABR_BROWSER_CONFIG, BrowserClickSolver
from jobfucker.clients.habr.captcha import HttpVisionSolver, LoginCaptcha
from jobfucker.clients.habr.config import HabrSearchEntry, HabrSearchType, HabrServiceConfig
from jobfucker.clients.habr.resumes import ResumeService
from jobfucker.clients.habr.search import SearchService
from jobfucker.clients.habr.transport import HabrTransport
from jobfucker.clients.shared.browser import BrowserDriver, PatchrightDriver

_HABR_PER_AUTH_APPLY_CAP = 150
_HABR_MAX_SEARCH_ITEMS = 1000


class HabrClient(Client):
    """HTTP-only Habr Career client with anonymous-read support."""

    service = "habr"
    service_info = ServiceInfo(
        service="habr",
        per_auth_apply_cap=_HABR_PER_AUTH_APPLY_CAP,
        apply_period="month",
        max_search_items=_HABR_MAX_SEARCH_ITEMS,
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
        if not isinstance(section, HabrServiceConfig):
            raise TypeError(f"HabrClient expects HabrServiceConfig, got {type(section).__name__}")
        # Empty credentials are allowed: the client supports anonymous reads
        # (type=all listings and previews). Only authorized actions fail closed.
        self._deps = deps
        self._entries = section.searches
        self._transport = HabrTransport(transport=http_transport)
        captcha = LoginCaptcha(
            browser_solver=BrowserClickSolver(
                browser_driver
                if browser_driver is not None
                else PatchrightDriver(HABR_BROWSER_CONFIG, reporter=deps.reporter),
                max_attempts=section.captcha_max_attempts,
                reporter=deps.reporter,
            ),
            vision_solver=HttpVisionSolver(
                self._transport, deps.captcha_handler, max_attempts=section.captcha_max_attempts
            ),
            reporter=deps.reporter,
        )
        self._auth = AuthCoordinator(deps, self._transport, captcha, resume_id=section.resume_id)
        self._search = SearchService(self._transport)
        self._resumes = ResumeService()
        self._applications = ApplicationService(self._transport, delay=apply_delay)

    async def authorize(self) -> Result[None, ClientError]:
        """Ensure a persisted or newly-created Habr session is healthy."""
        return await self._ensure_authorized()

    async def search_vacancies(
        self,
        search_index: int,
        *,
        offset: int = 0,
        limit: int = 100,
        page_size: int = 100,
        exclude: frozenset[ServiceVacancyId] = frozenset(),
    ) -> Result[SearchSlice, ClientError]:
        """Resolve a pool entry and fetch one enriched listing slice.

        ``type=all`` entries run anonymously (no auth preflight at all);
        ``type=suitable`` entries authorize first and fail closed without
        credentials before any request is sent.
        """
        entry_result = self._entry(search_index)
        if entry_result.is_err:
            return Err(entry_result.unwrap_err())
        entry = entry_result.unwrap()
        guard = await self._guard_search(entry)
        if guard.is_err:
            return Err(guard.unwrap_err())
        return await self._search.search(
            entry.query,
            entry.filter.to_search_filters(),
            entry.search_type,
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
        """Resolve a pool entry and return one listing-only window (no enrichment)."""
        entry_result = self._entry(search_index)
        if entry_result.is_err:
            return Err(entry_result.unwrap_err())
        entry = entry_result.unwrap()
        guard = await self._guard_search(entry)
        if guard.is_err:
            return Err(guard.unwrap_err())
        return await self._search.list_vacancies(
            entry.query,
            entry.filter.to_search_filters(),
            entry.search_type,
            offset=offset,
            limit=limit,
            page_size=page_size,
        )

    async def get_resumes(self) -> Result[list[ResumeInfo], ClientError]:
        """Authorize, then report the account's single resume (the alias)."""
        authorized = await self._ensure_authorized()
        if authorized.is_err:
            return Err(authorized.unwrap_err())
        return await self._resumes.list_resumes(self._auth.identity)

    async def get_identity(self) -> Result[ServiceIdentity, ClientError]:
        """Authorize, then return the identity decoded by the ``/users/me`` healthcheck."""
        authorized = await self._ensure_authorized()
        if authorized.is_err:
            return Err(authorized.unwrap_err())
        return Ok(self._auth.identity)

    async def apply_to_vacancy(
        self,
        *,
        vacancy_id: ServiceVacancyId,
        message: str | None = None,
    ) -> Result[ApplyResult, ClientError]:
        """Authorize, then preflight and submit one application."""
        authorized = await self._ensure_authorized()
        if authorized.is_err:
            return Err(authorized.unwrap_err())
        return await self._applications.apply(vacancy_id=vacancy_id, message=message)

    async def aclose(self) -> None:
        """Release the owned HTTP transport; safe to call repeatedly."""
        await self._auth.aclose()

    def _entry(self, search_index: int) -> Result[HabrSearchEntry, ClientError]:
        """Resolve a pool index onto its search entry."""
        if not 0 <= search_index < len(self._entries):
            return Err(
                ConfigurationError(
                    message=(
                        f"Habr search index {search_index} is outside the pipeline's "
                        f"search pool of {len(self._entries)} entries"
                    )
                )
            )
        return Ok(self._entries[search_index])

    async def _guard_search(self, entry: HabrSearchEntry) -> Result[None, ClientError]:
        """Authorize a ``suitable`` search; ``all`` searches stay anonymous."""
        if entry.search_type is not HabrSearchType.SUITABLE:
            return Ok(None)
        return await self._ensure_authorized()

    async def _ensure_authorized(self) -> Result[None, ClientError]:
        """Authorize when credentials exist; fail closed when they are empty.

        Anonymous reads never reach here. Every authorized action and the
        ``suitable`` search guard do: with no resolvable credentials the client
        cannot log in, so the action fails with a configuration error before any
        request is made.
        """
        if not self._deps.credentials.login.strip() or not self._deps.credentials.password:
            return Err(ConfigurationError(message="Habr authorize requires login credentials"))
        return await self._auth.authorize()
