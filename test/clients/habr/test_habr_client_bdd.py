"""BDD acceptance for Habr client conformance to the board-neutral contract (test/AGENTS.md §3b)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from pytest_bdd import given, scenarios, then, when
from rusty_results.prelude import Result

from jobfucker.clients.base import (
    Client,
    ClientError,
    ConfigurationError,
    SearchSlice,
    ServiceConfigSection,
    ServiceInfo,
)
from jobfucker.clients.habr.client import HabrClient
from jobfucker.clients.habr.config import HabrSearchEntry, HabrServiceConfig
from jobfucker.clients.mock.params import MockSearchEntry, MockServiceConfig
from jobfucker.testing.step_runner import async_run
from test.clients.habr.browser_fake import FakeHabrBrowserDriver
from test.clients.habr.helpers import RecordingSolver, habr_deps, profile_id_for

scenarios("bdd/client.feature")


class StrictRouter:
    """Records requests, then fails if any request is made at all."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raise AssertionError(f"Unexpected Habr request: {request.method} {request.url}")


class RecordingCloseTransport(httpx.MockTransport):
    """Mock transport that counts how many times it was closed."""

    def __init__(self) -> None:
        super().__init__(self._never)
        self.close_calls = 0

    @staticmethod
    def _never(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"Unexpected Habr request: {request.method} {request.url}")

    async def aclose(self) -> None:
        self.close_calls += 1
        await super().aclose()


@dataclass(frozen=True, slots=True)
class ClientScenario:
    """Frozen setup: a client factory plus a request-history accessor."""

    make_client: Callable[[], HabrClient]
    requests: Callable[[], tuple[httpx.Request, ...]]


@dataclass(frozen=True, slots=True)
class CloseScenario:
    """Frozen setup: the client factory plus the recording transport to inspect."""

    client: ClientScenario
    transport: RecordingCloseTransport


@dataclass(frozen=True, slots=True)
class ConstructionOutcome:
    """Frozen outcome: the exception raised while constructing (if any)."""

    error: Exception | None


@dataclass(frozen=True, slots=True)
class InspectionOutcome:
    """Frozen outcome: what the constructed client advertises."""

    is_client: bool
    service_info: ServiceInfo


@dataclass(frozen=True, slots=True)
class SearchIndexOutcome:
    """Frozen outcome: the failed search result plus the request history."""

    result: Result[SearchSlice, ClientError]
    requests: tuple[httpx.Request, ...]


@dataclass(frozen=True, slots=True)
class CloseOutcome:
    """Frozen outcome: the underlying transport's close count."""

    close_calls: int


def _all_type_client_factory(tmp_path: Path, router: httpx.MockTransport) -> Callable[[], HabrClient]:
    data_dir = tmp_path / "data"
    entry = HabrSearchEntry(query="python")

    def make_client() -> HabrClient:
        return HabrClient(
            habr_deps(data_dir, profile_id_for(), RecordingSolver()),
            HabrServiceConfig(searches=(entry,)),
            http_transport=router,
            browser_driver=FakeHabrBrowserDriver(),
        )

    return make_client


@given("a mock service section", target_fixture="other_section")
def mock_section_step() -> ServiceConfigSection:
    return MockServiceConfig(resume_id="r", searches=(MockSearchEntry(query="q"),))


@given("a Habr client factory over an all-type search", target_fixture="client_scenario")
def all_type_factory_step(tmp_path: Path) -> ClientScenario:
    router = StrictRouter()
    return ClientScenario(
        make_client=_all_type_client_factory(tmp_path, httpx.MockTransport(router)),
        requests=lambda: tuple(router.requests),
    )


@given("a Habr client factory over an all-type search with a strict router", target_fixture="client_scenario")
def strict_factory_step(tmp_path: Path) -> ClientScenario:
    router = StrictRouter()
    return ClientScenario(
        make_client=_all_type_client_factory(tmp_path, httpx.MockTransport(router)),
        requests=lambda: tuple(router.requests),
    )


@given("a Habr client factory over a recording transport", target_fixture="close_scenario")
def recording_factory_step(tmp_path: Path) -> CloseScenario:
    transport = RecordingCloseTransport()
    return CloseScenario(
        client=ClientScenario(
            make_client=_all_type_client_factory(tmp_path, transport),
            requests=lambda: (),
        ),
        transport=transport,
    )


@when("a Habr client is constructed from it", target_fixture="construction_outcome")
def construct_step(tmp_path: Path, other_section: ServiceConfigSection) -> ConstructionOutcome:
    error: Exception | None = None
    try:
        HabrClient(habr_deps(tmp_path / "data", profile_id_for(), RecordingSolver()), other_section)
    except TypeError as exc:
        error = exc
    return ConstructionOutcome(error=error)


@when("the client is inspected", target_fixture="inspection_outcome")
def inspect_step(client_scenario: ClientScenario) -> InspectionOutcome:
    client = client_scenario.make_client()
    try:
        return InspectionOutcome(is_client=isinstance(client, Client), service_info=client.service_info)
    finally:
        async_run(client.aclose())


@when("the client searches with index 5", target_fixture="search_index_outcome")
def search_index_step(client_scenario: ClientScenario) -> SearchIndexOutcome:
    async def call() -> Result[SearchSlice, ClientError]:
        client = client_scenario.make_client()
        try:
            return await client.search_vacancies(5)
        finally:
            await client.aclose()

    return SearchIndexOutcome(result=async_run(call()), requests=client_scenario.requests())


@when("the client is closed twice", target_fixture="close_outcome")
def close_twice_step(close_scenario: CloseScenario) -> CloseOutcome:
    client = close_scenario.client.make_client()

    async def close_twice() -> None:
        await client.aclose()
        await client.aclose()

    async_run(close_twice())
    return CloseOutcome(close_calls=close_scenario.transport.close_calls)


@then("construction raises a type error")
def assert_type_error_step(construction_outcome: ConstructionOutcome) -> None:
    assert isinstance(construction_outcome.error, TypeError)


@then("the client is a board-neutral Client")
def assert_is_client_step(inspection_outcome: InspectionOutcome) -> None:
    assert inspection_outcome.is_client is True


@then("the service info reports 150 applications per month and 1000 search items")
def assert_service_info_step(inspection_outcome: InspectionOutcome) -> None:
    info = inspection_outcome.service_info
    assert info.service == "habr"
    assert info.per_auth_apply_cap == 150
    assert info.apply_period == "month"
    assert info.max_search_items == 1000


@then("the search fails with a configuration error")
def assert_configuration_error_step(search_index_outcome: SearchIndexOutcome) -> None:
    assert search_index_outcome.result.is_err
    assert isinstance(search_index_outcome.result.unwrap_err(), ConfigurationError)


@then("no Habr request is sent")
def assert_no_request_step(search_index_outcome: SearchIndexOutcome) -> None:
    assert search_index_outcome.requests == ()


@then("the underlying transport is closed exactly once")
def assert_closed_once_step(close_outcome: CloseOutcome) -> None:
    assert close_outcome.close_calls == 1
