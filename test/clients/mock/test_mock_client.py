"""Phase 2 (task 2.3): behavioral tests for the first-party Mock client.

Covers every programmable behavior (``applied``/``skipped``/``error``/
``limit_exceeded`` + a failing ``authorize``) and the client's construction
contract (constructed from ``ClientDeps`` + a ``MockServiceConfig`` section
exactly like a real client). Pure client unit behavior, so plain pytest per
the harness convention (test/AGENTS.md §3).

The canned vacancy dataset is **not** inlined here and not in the client code:
the sections under test are seeded from ``test/fixtures/mock_vacancies.yaml``
(via :func:`test.pipeline_helpers.mock_vacancies`), so editing that fixture is
what changes the search results these tests assert — keep the two in sync.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Literal, override

import pytest
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    ApplyFailed,
    ApplyResult,
    ApplySkipped,
    ApplySucceeded,
    AuthError,
    CaptchaHandler,
    CaptchaSolvingError,
    Client,
    ClientDeps,
    ClientError,
    LimitExceededError,
    ServiceVacancyId,
    TransportError,
    Vacancy,
    VacancyShort,
)
from jobfucker.clients.mock.client import MockClient
from jobfucker.clients.mock.params import (
    MockApplyBehaviorConfig,
    MockBehaviorConfig,
    MockCaptchaConfig,
    MockOutcome,
    MockSearchEntry,
    MockSearchParams,
    MockServiceConfig,
    MockVacancyParams,
    MockVacancySalary,
)
from jobfucker.clients.paging import FetchedPage
from test.pipeline_helpers import mock_vacancies

# The mock client's declared per-auth daily cap (asserted in conformance).
_MOCK_CAP = 200
# Minimum full-description length proving enrichment (no stub snippet).
_MIN_DESCRIPTION_LEN = 200
# The canned vacancies' ids, in search order — sourced from the SAME fixture
# (test/fixtures/mock_vacancies.yaml) the sections below are seeded from, so the
# assertion can never drift from the data.
_DEFAULT_VACANCY_IDS: tuple[str, ...] = tuple(v.external_id for v in mock_vacancies())


def _params() -> MockSearchParams:
    """Build the mock's concrete search filter for the ``filter`` field."""
    return MockSearchParams(
        area=(1,),
        schedule=("fullDay",),
        experience="between1And3",
        only_with_salary=True,
    )


def _section(behavior: MockBehaviorConfig | None = None) -> MockServiceConfig:
    """Build a mock service section; ``behavior`` is an optional seam.

    The section is seeded with the canned vacancies from
    ``test/fixtures/mock_vacancies.yaml`` (see :func:`mock_vacancies`), so the
    default search returns exactly the fixture's entries.
    """
    return MockServiceConfig(
        resume_id="mock-resume-1",
        searches=(
            MockSearchEntry(
                query="python",
                filter=_params(),
                vacancies=mock_vacancies(),
            ),
        ),
        behavior=behavior,
    )


def _apply_behavior(outcome: MockOutcome, message: str | None = None) -> MockApplyBehaviorConfig:
    """A single programmable apply outcome for the mock's ``behavior`` block."""
    return MockApplyBehaviorConfig(outcome=outcome, message=message)


class _FailOnPageMockClient(MockClient):
    """A mock whose native page ``fail_page`` fails exactly like a transport error.

    Fault injection for the client-side paging semantics: overrides the mock's
    per-page seam so the shared driver sees one failing page mid-walk.
    """

    def __init__(self, deps: ClientDeps, section: MockServiceConfig, *, fail_page: int, message: str) -> None:
        super().__init__(deps, section)
        self._fail_page: int = fail_page
        self._fail_message: str = message

    @override
    async def _fetch_listing_page(
        self,
        canned: tuple[Vacancy, ...],
        page: int,
        keep_start: int,
        keep_end: int,
        *,
        page_size: int,
    ) -> Result[FetchedPage, ClientError]:
        if page == self._fail_page:
            return Err(TransportError(message=self._fail_message))
        return await super()._fetch_listing_page(canned, page, keep_start, keep_end, page_size=page_size)


@pytest.mark.unit
def test_captcha_image_read_once_per_process(client_deps: ClientDeps, monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing several MockClients reads each canned file at most once.

    The mock's only canned file is its captcha PNG (vacancies come from the
    ``service.mock`` section, so no vacancy file is ever read). The loaders run
    on the event loop (``MockClient`` is constructed inside engine run paths),
    so the blocking read + parse is ``functools.cache``d per process: each
    fixture file is read at most once no matter how many clients are
    constructed. Two constructions must never produce two reads — removing the
    cache makes this test fail with two reads.

    The assertion is intentionally ``<= 1`` rather than ``== 1``: the
    process-wide cache may already be warm from an earlier test in this run, in
    which case zero reads is the correct outcome.
    """
    read_names: list[str] = []
    original_read_bytes = Path.read_bytes

    def _counting_read_bytes(self: Path) -> bytes:
        read_names.append(self.name)
        return original_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _counting_read_bytes)

    MockClient(client_deps, section=_section())
    MockClient(client_deps, section=_section())

    assert read_names.count("captcha.png") <= 1
    assert "mock_vacancies.json" not in read_names  # vacancies come from the section, never a file


@pytest.mark.unit
async def test_constructible_from_client_deps(client_deps: ClientDeps) -> None:
    """MockClient is built from ClientDeps + its own section, like any real client."""
    client = MockClient(client_deps, section=_section())
    assert isinstance(client, Client)
    assert client.service == "mock"
    assert client.service_info.per_auth_daily_cap == _MOCK_CAP


@pytest.mark.unit
async def test_authorize_succeeds_by_default(client_deps: ClientDeps) -> None:
    """``authorize`` is a no-op success unless programmed to fail."""
    result = await MockClient(client_deps, section=_section()).authorize()
    assert result.is_ok


@pytest.mark.unit
async def test_authorize_can_fail_with_auth_error(client_deps: ClientDeps) -> None:
    """``authorize`` returns ``Err(AuthError)`` when told to fail via behavior."""
    behavior = MockBehaviorConfig(authorize_error="mock auth failed")
    client = MockClient(client_deps, section=_section(behavior))
    result = await client.authorize()
    assert result.is_err
    assert isinstance(result.unwrap_err(), AuthError)
    assert result.unwrap_err().message == "mock auth failed"


@pytest.mark.unit
async def test_search_returns_full_plaintext_descriptions(client_deps: ClientDeps) -> None:
    """``search_vacancies(query)`` (defaults) returns vacancies with full enriched descriptions."""
    # Seeded from test/fixtures/mock_vacancies.yaml via ``mock_vacancies()``.
    client = MockClient(client_deps, section=_section())
    result = await client.search_vacancies(0)
    assert result.is_ok
    listing = result.unwrap()
    vacancies = listing.items
    assert len(vacancies) > 0
    assert all(isinstance(v, Vacancy) for v in vacancies)
    # Full plaintext: description is multi-sentence prose, never a stub snippet.
    assert all(len(v.description) > _MIN_DESCRIPTION_LEN for v in vacancies)
    # The section seeded from test/fixtures/mock_vacancies.yaml returns that
    # fixture's three entries in file order.
    assert [v.external_id for v in vacancies] == list(_DEFAULT_VACANCY_IDS)


@pytest.mark.unit
async def test_search_returns_section_vacancies(client_deps: ClientDeps) -> None:
    """The mock's vacancies come from ``section.vacancies`` — never from a canned file.

    A section that declares its own ``vacancies`` list fully drives the search
    results (including the salary's ``from`` lower bound via its alias). This is
    the escape hatch from the fixture-backed default list
    (``test/fixtures/mock_vacancies.yaml``) this module's ``_section`` seeds.
    """
    section = MockServiceConfig(
        resume_id="mock-resume-1",
        searches=(
            MockSearchEntry(
                query="python",
                filter=_params(),
                vacancies=[
                    MockVacancyParams(
                        external_id="custom-1",
                        title="Custom Role (Section)",
                        url="https://mock.example/vacancies/custom-1",
                        company="SectionCo",
                        description="A single vacancy declared in the service.mock section.",
                        key_skills=["Python"],
                        salary=MockVacancySalary(from_=120000, to=180000, currency="RUR", gross=False),
                    )
                ],
            ),
        ),
    )
    client = MockClient(client_deps, section=section)
    result = await client.search_vacancies(0)
    assert result.is_ok
    vacancies = result.unwrap().items
    assert [v.external_id for v in vacancies] == ["custom-1"]
    assert vacancies[0].company == "SectionCo"
    assert vacancies[0].salary is not None
    assert vacancies[0].salary.from_ == 120000
    assert vacancies[0].salary.to == 180000
    assert vacancies[0].salary.currency == "RUR"


@pytest.mark.unit
async def test_search_honours_slice_window(client_deps: ClientDeps) -> None:
    """Offset/limit/page_size controls select the expected listing slice."""
    client = MockClient(client_deps, section=_section())
    first = (await client.search_vacancies(0, offset=0, limit=1, page_size=1)).unwrap()
    second = (await client.search_vacancies(0, offset=1, limit=1, page_size=1)).unwrap()
    assert len(first.items) == 1
    assert len(second.items) == 1
    assert first.items[0].external_id != second.items[0].external_id
    # Slice metadata: one planned/scanned page, not exhausted (listing has more).
    assert first.offset == 0
    assert first.pages_scanned == 1
    assert first.exhausted is False


@pytest.mark.unit
async def test_search_reports_partial_slice_on_page_failure(client_deps: ClientDeps) -> None:
    """A failing native page yields a partial slice with a failure descriptor."""
    section = MockServiceConfig(
        resume_id="mock-resume-1",
        searches=(
            MockSearchEntry(
                query="python",
                filter=_params(),
                vacancies=[
                    MockVacancyParams(
                        external_id=f"bulk-{index}",
                        title=f"Bulk {index}",
                        url=f"https://mock.example/vacancies/bulk-{index}",
                        company="BulkCo",
                        description="Bulk canned vacancy for the paging failure path.",
                        key_skills=["Python"],
                        salary=None,
                    )
                    for index in range(3)
                ],
            ),
        ),
    )
    client = _FailOnPageMockClient(client_deps, section, fail_page=1, message="boom")

    result = await client.search_vacancies(0, offset=0, limit=3, page_size=2)

    assert result.is_ok  # partial result, never a bare Err for a page failure
    listing = result.unwrap()
    assert [v.external_id for v in listing.items] == ["bulk-0", "bulk-1"]
    assert listing.failure is not None
    assert listing.failure.page == 1
    assert listing.failure.error.message == "boom"
    assert listing.pages_scanned == 1
    assert listing.pages_planned == 2
    assert listing.exhausted is False


@pytest.mark.unit
async def test_list_vacancies_projects_canned_vacancies_without_enrichment(client_deps: ClientDeps) -> None:
    """``list_vacancies`` returns short items in config order; the mock has no web page."""
    client = MockClient(client_deps, section=_section())
    result = await client.list_vacancies(0)
    assert result.is_ok
    listing = result.unwrap()
    assert [v.external_id for v in listing.items] == list(_DEFAULT_VACANCY_IDS)
    first = listing.items[0]
    assert isinstance(first, VacancyShort)
    assert first.company is not None  # projected from the canned vacancy
    # Envelope metadata: the canned total, and no UI URL (the mock has no web page).
    assert listing.found == len(_DEFAULT_VACANCY_IDS)
    assert listing.ui_url is None
    assert listing.offset == 0


@pytest.mark.unit
async def test_list_vacancies_honours_window(client_deps: ClientDeps) -> None:
    """Offset/limit/page_size select the expected short-item window."""
    client = MockClient(client_deps, section=_section())
    second = (await client.list_vacancies(0, offset=1, limit=1, page_size=1)).unwrap()
    assert len(second.items) == 1
    assert second.items[0].external_id == _DEFAULT_VACANCY_IDS[1]
    assert second.pages_scanned == 1


@pytest.mark.unit
async def test_list_vacancies_page_failure_is_a_plain_error(client_deps: ClientDeps) -> None:
    """A failing page on the listing path is a plain Err (no partial Ok)."""
    section = MockServiceConfig(
        resume_id="mock-resume-1",
        searches=(
            MockSearchEntry(
                query="python",
                filter=_params(),
                vacancies=[
                    MockVacancyParams(
                        external_id=f"bulk-{index}",
                        title=f"Bulk {index}",
                        url=f"https://mock.example/vacancies/bulk-{index}",
                        company="BulkCo",
                        description="Bulk canned vacancy for the listing failure path.",
                        key_skills=["Python"],
                        salary=None,
                    )
                    for index in range(3)
                ],
            ),
        ),
    )
    client = _FailOnPageMockClient(client_deps, section, fail_page=1, message="boom")

    result = await client.list_vacancies(0, offset=0, limit=3, page_size=2)

    assert result.is_err
    assert isinstance(result.unwrap_err(), TransportError)


@pytest.mark.unit
async def test_list_vacancies_captcha_fires_once_per_call(client_deps: ClientDeps) -> None:
    """A captcha scripted on ``search`` gates the listing path too (once per call)."""
    handler, images = _counting_handler(solved=True)
    client = MockClient(_deps_with_handler(client_deps, handler), section=_section(_captcha_behavior("search")))
    result = await client.list_vacancies(0)
    assert result.is_ok
    assert len(result.unwrap().items) > 0
    assert len(images) == 1


@pytest.mark.unit
async def test_get_resumes_returns_mock_resume(client_deps: ClientDeps) -> None:
    """``get_resumes`` returns a canned resume list."""
    result = await MockClient(client_deps, section=_section()).get_resumes()
    assert result.is_ok
    assert result.unwrap()[0].resume_id == "mock-resume-1"


@pytest.mark.unit
async def test_apply_default_applied_no_behavior(client_deps: ClientDeps) -> None:
    """With no behavior, apply is ``applied`` (Ok)."""
    client = MockClient(client_deps, section=_section())
    result = await client.apply_to_vacancy(resume_id="mock-resume-1", vacancy_id=ServiceVacancyId("mock-1"))
    assert result.is_ok
    assert isinstance(result.unwrap(), ApplySucceeded)


@pytest.mark.unit
async def test_apply_default_applied_when_default_apply_is_none(client_deps: ClientDeps) -> None:
    """A behavior block with ``default_apply=None`` still yields ``applied`` (guards the None-able default)."""
    client = MockClient(client_deps, section=_section(MockBehaviorConfig()))
    result = await client.apply_to_vacancy(resume_id="mock-resume-1", vacancy_id=ServiceVacancyId("mock-1"))
    assert result.is_ok
    assert isinstance(result.unwrap(), ApplySucceeded)


@pytest.mark.unit
async def test_apply_skipped(client_deps: ClientDeps) -> None:
    """A programmed ``skipped`` default maps to an ``ApplyResult`` skip."""
    behavior = MockBehaviorConfig(default_apply=_apply_behavior("skipped", message="below threshold"))
    client = MockClient(client_deps, section=_section(behavior))
    result = await client.apply_to_vacancy(resume_id="mock-resume-1", vacancy_id=ServiceVacancyId("mock-1"))
    assert result.is_ok
    outcome: ApplyResult = result.unwrap()
    assert isinstance(outcome, ApplySkipped)
    assert outcome.skip.text == "below threshold"


@pytest.mark.unit
async def test_apply_error(client_deps: ClientDeps) -> None:
    """A programmed ``error`` default surfaces as a deterministic ``ApplyFailed``."""
    behavior = MockBehaviorConfig(default_apply=_apply_behavior("error", message="bad request"))
    client = MockClient(client_deps, section=_section(behavior))
    result = await client.apply_to_vacancy(resume_id="mock-resume-1", vacancy_id=ServiceVacancyId("mock-1"))
    assert result.is_ok
    outcome = result.unwrap()
    assert isinstance(outcome, ApplyFailed)
    assert outcome.error.text == "bad request"


@pytest.mark.unit
async def test_apply_limit_exceeded(client_deps: ClientDeps) -> None:
    """``limit_exceeded`` is the stop signal ``Err(LimitExceededError)``."""
    behavior = MockBehaviorConfig(per_vacancy={"mock-2": _apply_behavior("limit_exceeded", message="cap reached")})
    client = MockClient(client_deps, section=_section(behavior))
    result = await client.apply_to_vacancy(resume_id="mock-resume-1", vacancy_id=ServiceVacancyId("mock-2"))
    assert result.is_err
    error = result.unwrap_err()
    assert isinstance(error, LimitExceededError)
    assert error.message == "cap reached"


@pytest.mark.unit
async def test_per_vacancy_override_beats_default(client_deps: ClientDeps) -> None:
    """``per_vacancy`` override takes precedence over ``default_apply``; unlisted ids fall back."""
    behavior = MockBehaviorConfig(
        per_vacancy={"mock-1": _apply_behavior("skipped")},
        default_apply=_apply_behavior("applied"),
    )
    client = MockClient(client_deps, section=_section(behavior))
    ok = await client.apply_to_vacancy(resume_id="mock-resume-1", vacancy_id=ServiceVacancyId("mock-1"))
    assert ok.is_ok and isinstance(ok.unwrap(), ApplySkipped)
    other = await client.apply_to_vacancy(resume_id="mock-resume-1", vacancy_id=ServiceVacancyId("mock-9"))
    assert other.is_ok and isinstance(other.unwrap(), ApplySucceeded)


@pytest.mark.unit
async def test_applied_returns_closed_success_variant(client_deps: ClientDeps) -> None:
    """A ``applied`` outcome exposes no unrelated optional fields."""
    client = MockClient(client_deps, section=_section())
    result = await client.apply_to_vacancy(
        resume_id="mock-resume-1",
        vacancy_id=ServiceVacancyId("mock-1"),
        message="cover letter text",
    )
    assert result.is_ok
    assert isinstance(result.unwrap(), ApplySucceeded)


# --- captcha emission (manual-testing seam) -----------------------------------
def _counting_handler(solved: bool) -> tuple[CaptchaHandler, list[bytes]]:
    """A captcha handler stub that records the images it received."""
    images: list[bytes] = []

    async def _handler(image: bytes) -> Result[str, str]:
        images.append(image)
        if not solved:
            return Err("captcha cancelled (EOF)")
        return Ok("stub-captcha")

    return _handler, images


def _deps_with_handler(client_deps: ClientDeps, handler: CaptchaHandler) -> ClientDeps:
    """Swap the stub captcha handler for a custom one (keeps other deps)."""
    return replace(client_deps, captcha_handler=handler)


def _captcha_behavior(operation: Literal["authorize", "search", "apply"], attempts: int = 1) -> MockBehaviorConfig:
    """A behavior block scripting a captcha on one mock operation."""
    return MockBehaviorConfig(captcha=MockCaptchaConfig(operation=operation, attempts=attempts))


@pytest.mark.unit
async def test_apply_captcha_solved_then_proceeds(client_deps: ClientDeps) -> None:
    """A solved captcha on ``apply`` still yields the configured apply outcome."""
    handler, images = _counting_handler(solved=True)
    client = MockClient(_deps_with_handler(client_deps, handler), section=_section(_captcha_behavior("apply")))
    result = await client.apply_to_vacancy(resume_id="mock-resume-1", vacancy_id=ServiceVacancyId("mock-1"))
    assert result.is_ok
    assert isinstance(result.unwrap(), ApplySucceeded)
    # The injected captcha handler saw the canned captcha PNG exactly once.
    assert len(images) == 1
    assert images[0].startswith(b"\x89PNG")


@pytest.mark.unit
async def test_apply_captcha_unsolvable_is_captcha_solving_error(client_deps: ClientDeps) -> None:
    """An unsolvable captcha collapses into ``Err(CaptchaSolvingError)``, never a crash."""
    handler, images = _counting_handler(solved=False)
    client = MockClient(_deps_with_handler(client_deps, handler), section=_section(_captcha_behavior("apply")))
    result = await client.apply_to_vacancy(resume_id="mock-resume-1", vacancy_id=ServiceVacancyId("mock-1"))
    assert result.is_err
    error = result.unwrap_err()
    assert isinstance(error, CaptchaSolvingError)
    assert "captcha cancelled (EOF)" in error.message
    assert len(images) == 1


@pytest.mark.unit
async def test_apply_captcha_attempts_loop(client_deps: ClientDeps) -> None:
    """``attempts`` requires solving the challenge that many times before proceeding."""
    handler, images = _counting_handler(solved=True)
    client = MockClient(
        _deps_with_handler(client_deps, handler),
        section=_section(_captcha_behavior("apply", attempts=3)),
    )
    result = await client.apply_to_vacancy(resume_id="mock-resume-1", vacancy_id=ServiceVacancyId("mock-1"))
    assert result.is_ok
    assert len(images) == 3


@pytest.mark.unit
async def test_search_captcha_fires_on_search(client_deps: ClientDeps) -> None:
    """A captcha scripted on ``search`` gates the canned vacancy listing.

    The challenge fires once per slice call (not per native page) — enough to
    exercise the human-solve path during fetch.
    """
    handler, images = _counting_handler(solved=True)
    client = MockClient(_deps_with_handler(client_deps, handler), section=_section(_captcha_behavior("search")))
    result = await client.search_vacancies(0)
    assert result.is_ok
    assert len(result.unwrap().items) > 0
    assert len(images) == 1


@pytest.mark.unit
async def test_captcha_not_fired_when_operation_mismatches(client_deps: ClientDeps) -> None:
    """A captcha scripted on one operation never fires on another."""
    handler, images = _counting_handler(solved=True)
    client = MockClient(_deps_with_handler(client_deps, handler), section=_section(_captcha_behavior("search")))
    result = await client.apply_to_vacancy(resume_id="mock-resume-1", vacancy_id=ServiceVacancyId("mock-1"))
    assert result.is_ok
    assert images == []


@pytest.mark.unit
async def test_no_behavior_never_calls_captcha_handler(client_deps: ClientDeps) -> None:
    """Without a ``captcha`` behavior the injected handler is never invoked."""
    handler, images = _counting_handler(solved=True)
    client = MockClient(_deps_with_handler(client_deps, handler), section=_section())
    result = await client.authorize()
    assert result.is_ok
    assert images == []
