"""Canonical in-memory ``Client`` double used by engine/CLI/UI tests.

This is the **living conformance check** for the frozen client contract
(``jobfucker.clients.base``): it is a second, in-core consumer of the ``Client``
protocol with canned data and programmable behaviors. A contract change that
drops, renames, or retypes a ``Client`` member without mirroring it here breaks
``test/contract/test_fake_conformance.py`` (and therefore ``uv run poe test``) —
the smoke signal that the two tracks are out of sync (client-contract.md
"Stability & governance").

It is intentionally a **standalone** double (it does not compose over
``MockClient``) so it exercises the protocol independently and a drift in the
contract is caught twice. It lives under ``test/`` (not ``tests/``) per the
local test-dir convention.

It reflects the frozen contract: the ``Client`` protocol is **non-generic**
(there is no ``SearchParams``), every client is constructed from ``(deps,
section)`` (``ClientDeps`` + the board's typed ``service.<board>`` section), and
the client's per-auth daily cap is **class-level metadata** read via
``Factory.cap`` without constructing a client. Accordingly ``FakeClient`` is
built from a ``(deps, section)`` pair, self-configures from its section, and
carries ``service``/``service_info`` as class attributes so ``Factory.cap``
works with no construction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    ApplyError,
    ApplyFailed,
    ApplyResult,
    ApplySkip,
    ApplySkipped,
    ApplySucceeded,
    AuthError,
    Client,
    ClientDeps,
    ClientError,
    ConfigurationError,
    LimitExceededError,
    ResumeInfo,
    Salary,
    SearchListing,
    SearchSlice,
    SearchWindow,
    ServiceConfigSection,
    ServiceIdentity,
    ServiceInfo,
    ServiceVacancyId,
    Vacancy,
    VacancyShort,
)
from jobfucker.clients.paging import FetchedListingPage, FetchedPage, scan_listing, scan_slice
from jobfucker.reporting import EventLevel, RunEvent

_FAKE_CAP = 200


@dataclass(frozen=True, slots=True)
class FakeApplyBehavior:
    """A single programmable apply outcome for the fake (mirrors the mock)."""

    outcome: Literal["applied", "skipped", "error", "limit_exceeded", "config_error", "auth_error"]
    message: str | None = None


@dataclass(frozen=True, slots=True)
class FakeBehavior:
    """Programmable behaviors a :class:`FakeClient` is scripted with."""

    default_apply: FakeApplyBehavior = field(default_factory=lambda: FakeApplyBehavior("applied"))
    per_vacancy: Mapping[ServiceVacancyId, FakeApplyBehavior] = field(
        default_factory=lambda: dict[ServiceVacancyId, FakeApplyBehavior]()
    )
    authorize_error: str | None = None


@dataclass(frozen=True, slots=True)
class FakeSearchEntry:
    """One pool entry of the fake's typed section (mirrors the contract base)."""

    query: str = "fake query"
    filter: object | None = None  # lint-ignore[restricted-object]: the fake's board filter is never read
    window: SearchWindow | None = None


@dataclass(frozen=True, slots=True)
class FakeServiceConfig:
    """The fake's typed ``service.<board>`` section (implements the protocol).

    ``resume_id`` is the one field every board's section carries
    (``ServiceConfigSection``) and ``searches`` is the ordered pool core
    iterates (one :class:`FakeSearchEntry` by default); optional ``behavior``
    lets the fake self-configure its programmable behaviors straight from the
    config section, mirroring how a real client derives its construction
    behavior from its typed section.
    """

    resume_id: str
    searches: tuple[FakeSearchEntry, ...] = (FakeSearchEntry(),)
    behavior: FakeBehavior | None = None


def make_fake_vacancy(
    external_id: str,
    *,
    title: str = "Fake Vacancy",
    company: str | None = "FakeCo",
) -> Vacancy:
    """Build a canned :class:`Vacancy` with a full plaintext description."""
    return Vacancy(
        external_id=ServiceVacancyId(external_id),
        title=title,
        url=f"https://fake.example/vacancies/{external_id}",
        company=company,
        description="Full plaintext description used by engine/CLI/UI tests.",
        key_skills=("Python",),
        salary=Salary(from_=100_000, to=200_000, currency="RUR", gross=False),
    )


_DEFAULT_VACANCIES: tuple[Vacancy, ...] = (
    make_fake_vacancy("fake-1", title="Fake Backend"),
    make_fake_vacancy("fake-2", title="Fake DevOps"),
)


def _to_vacancy_short(vacancy: Vacancy) -> VacancyShort:
    """Project a canned ``Vacancy`` onto the short listing form (enriched by construction)."""
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


_DEFAULT_RESUMES: tuple[ResumeInfo, ...] = (
    ResumeInfo(resume_id="fake-resume-1", title="Fake Resume", updated_at=None),
)

_DEFAULT_SECTION = FakeServiceConfig(resume_id="fake-resume-1")


class FakeClient(Client):
    """A scriptable in-memory double implementing the ``Client`` protocol.

    Construct it exactly like a real client — from a ``(deps, section)`` pair —
    with optional canned data and programmable behaviors; defaults are
    "everything succeeds". ``service``/``service_info`` are **class attributes**
    so ``Factory.cap`` reads the fake's per-auth daily cap without constructing
    one. Used by engine/CLI/UI tests that need a client without a live board.
    """

    service: str = "fake"
    service_info: ServiceInfo = ServiceInfo(
        service="fake",
        per_auth_daily_cap=_FAKE_CAP,
        max_search_items=None,  # the fake's canned listing has no search cap
    )

    def __init__(
        self,
        deps: ClientDeps,
        section: ServiceConfigSection,
        *,
        vacancies: tuple[Vacancy, ...] | None = None,
        resumes: tuple[ResumeInfo, ...] | None = None,
        behavior: FakeBehavior | None = None,
    ) -> None:
        """Configure the double.

        Args:
            deps: the standard :class:`ClientDeps` (accepted for signature
                parity with real clients; the fake does not touch the network).
            section: the typed ``service.<board>`` section the fake
                self-configures from (resume id, optional scripting behavior).
            vacancies: canned vacancies returned by ``search_vacancies``.
            resumes: canned resumes returned by ``get_resumes``.
            behavior: optional programmable apply/authorize behaviors
                (all-succeed default, overriding any ``section.behavior``).
        """
        self._deps: ClientDeps = deps
        self._section: FakeServiceConfig = _coerce_section(section)
        self._vacancies: tuple[Vacancy, ...] = vacancies if vacancies is not None else _DEFAULT_VACANCIES
        self._resumes: tuple[ResumeInfo, ...] = resumes if resumes is not None else _DEFAULT_RESUMES
        section_behavior = self._section.behavior
        self._behavior: FakeBehavior = (
            behavior if behavior is not None else (section_behavior if section_behavior is not None else FakeBehavior())
        )

    async def _report(self, message: str, *, level: EventLevel = "debug") -> None:
        """Publish a board-operation event on the shared sink (debug detail)."""
        await self._deps.reporter.publish(
            RunEvent(
                stage="client",
                message=message,
                level=level,
            )
        )

    async def authorize(self) -> Result[None, ClientError]:
        """Succeed (no-op) unless ``authorize_error`` is set."""
        if self._behavior.authorize_error is not None:
            await self._report("authorize: failed", level="error")
            return Err(AuthError(message=self._behavior.authorize_error))
        await self._report("authorize: ok")
        return Ok(None)

    async def search_vacancies(
        self,
        search_index: int,
        *,
        offset: int = 0,
        limit: int = 100,
        page_size: int = 100,
        exclude: frozenset[ServiceVacancyId] = frozenset(),
    ) -> Result[SearchSlice, ClientError]:
        """Return a slice of the canned vacancies via the shared paging driver."""
        del search_index
        canned = self._vacancies

        async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedPage, ClientError]:
            page_start = page * page_size
            window = canned[page_start : page_start + page_size]
            kept = window[max(0, keep_start - page_start) : max(0, keep_end - page_start)]
            return Ok(FetchedPage(kept=tuple(kept), listed=len(window)))

        return await scan_slice(fetch_page, offset=offset, limit=limit, page_size=page_size, exclude=exclude)

    async def list_vacancies(
        self,
        search_index: int,
        *,
        offset: int = 0,
        limit: int = 100,
        page_size: int = 100,
    ) -> Result[SearchListing, ClientError]:
        """Return a window of the canned vacancies as short items (no enrichment)."""
        del search_index
        canned = self._vacancies
        canned_total = len(canned)

        async def fetch_page(page: int, keep_start: int, keep_end: int) -> Result[FetchedListingPage, ClientError]:
            page_start = page * page_size
            window = canned[page_start : page_start + page_size]
            kept = window[max(0, keep_start - page_start) : max(0, keep_end - page_start)]
            return Ok(
                FetchedListingPage(
                    kept=tuple(_to_vacancy_short(vacancy) for vacancy in kept),
                    listed=len(window),
                    found=canned_total,
                    ui_url=None,
                )
            )

        return await scan_listing(fetch_page, offset=offset, limit=limit, page_size=page_size)

    async def get_resumes(self) -> Result[list[ResumeInfo], ClientError]:
        """Return the canned resumes."""
        return Ok(list(self._resumes))

    async def get_identity(self) -> Result[ServiceIdentity, ClientError]:
        """Return the canned fake account identity."""
        return Ok(ServiceIdentity(external_id="fake-user-1", display_name="Fake User", email="fake-user@example.com"))

    async def apply_to_vacancy(
        self,
        *,
        resume_id: str,
        vacancy_id: ServiceVacancyId,
        message: str | None = None,
    ) -> Result[ApplyResult, ClientError]:
        """Apply, honouring the programmable per-vacancy/default behavior."""
        del resume_id
        await self._report(f"apply: sending to vacancy #{vacancy_id}")
        behavior = self._behavior.per_vacancy.get(vacancy_id, self._behavior.default_apply)
        result: Result[ApplyResult, ClientError]
        match behavior.outcome:
            case "applied":
                result = Ok(ApplySucceeded())
            case "skipped":
                result = Ok(
                    ApplySkipped(
                        ApplySkip(
                            reason="vacancy_unavailable",
                            text=behavior.message or "fake application skipped",
                        )
                    )
                )
            case "error":
                result = Ok(ApplyFailed(ApplyError(text=behavior.message or "fake apply rejected")))
            case "limit_exceeded":
                result = Err(LimitExceededError(message=behavior.message or "fake per-auth daily cap reached"))
            case "config_error":
                result = Err(ConfigurationError(message=behavior.message or "fake configured resume rejected"))
            case "auth_error":
                result = Err(AuthError(message=behavior.message or "fake authorization rejected"))
        del message
        if result.is_ok and isinstance(result.unwrap(), ApplySucceeded):
            await self._report(f"apply: ok for vacancy #{vacancy_id}", level="success")
        return result

    async def aclose(self) -> None:
        """Release owned resources; the in-memory fake owns none."""


def _coerce_section(section: ServiceConfigSection) -> FakeServiceConfig:
    """Narrow a ``ServiceConfigSection`` to the fake's typed section.

    The factory hands every client its config section as the ``Client``-side
    protocol type; the fake unwraps that to its concrete section so it can read
    ``resume_id``/``behavior`` with full type safety.
    """
    if isinstance(section, FakeServiceConfig):
        return section
    return FakeServiceConfig(resume_id=section.resume_id)
