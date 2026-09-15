"""Phase 2 (task 2.2): contract-boundary conformance for the client factory.

Proves the registered Mock client satisfies the frozen ``Client`` protocol
(via ``@runtime_checkable``), that ``get``/``cap`` resolve the registry as
documented, and that an unknown ``service`` (or a section-less factory) raises
fail-fast with a clear message — per client-contract.md §6.
"""

from __future__ import annotations

import pytest

from jobfucker.clients.base import Client, ClientDeps
from jobfucker.clients.factory import Factory
from jobfucker.clients.mock.client import MockClient
from jobfucker.clients.mock.params import MockSearchEntry, MockSearchParams, MockServiceConfig

# The mock client's declared per-auth daily cap (asserted in conformance).
_MOCK_CAP = 200


@pytest.fixture
def factory(client_deps: ClientDeps) -> Factory:
    """A factory with a bound ``service.mock`` section and the ``mock`` client.

    The section is bound at construction (as the composition root does) and the
    client class is registered without any params builder — the client
    self-configures from the bound section on ``get``.
    """
    f = Factory(client_deps, section=_mock_section())
    f.register("mock", MockClient)
    return f


def _mock_section() -> MockServiceConfig:
    """A valid typed ``service.mock`` section (resume_id + filter)."""
    return MockServiceConfig(
        resume_id="mock-resume-1",
        searches=(
            MockSearchEntry(
                query="python",
                filter=MockSearchParams(
                    area=(1,),
                    schedule=("fullDay",),
                    experience="between1And3",
                    only_with_salary=True,
                ),
            ),
        ),
    )


def test_registered_mock_satisfies_client_protocol(factory: Factory) -> None:
    """The registered Mock client is a ``Client`` with the expected metadata."""
    client = factory.get("mock")
    assert isinstance(client, Client)
    assert client.service == "mock"
    assert client.service_info.per_auth_daily_cap == _MOCK_CAP


def test_get_returns_a_constructed_client(factory: Factory) -> None:
    """``get`` constructs a real MockClient from the factory's deps + section."""
    client = factory.get("mock")
    assert isinstance(client, MockClient)


def test_get_unknown_service_raises_fail_fast(factory: Factory) -> None:
    """Unknown ``service`` is a programmer/config error → ``KeyError``."""
    with pytest.raises(KeyError, match="Unknown service 'nope'"):
        factory.get("nope")


def test_get_on_section_less_factory_raises(client_deps: ClientDeps) -> None:
    """A factory with no bound config section cannot construct a client.

    ``section`` is optional at construction, but ``get`` must fail fast
    (``RuntimeError``) if no section was ever bound — a config-less factory
    cannot build a real client.
    """
    f = Factory(client_deps)
    f.register("mock", MockClient)
    with pytest.raises(RuntimeError, match="no bound config section"):
        f.get("mock")


def test_cap_returns_client_cap_without_constructing(client_deps: ClientDeps) -> None:
    """``cap`` reads the client's class-level cap, needing no construction.

    ``cap`` must work even on a section-less factory, proving it reads the
    class-level ``service_info.per_auth_daily_cap`` rather than constructing a
    client (which a section-less factory could not do).
    """
    f = Factory(client_deps)
    f.register("mock", MockClient)
    assert f.cap("mock") == _MOCK_CAP


def test_cap_unknown_service_raises(client_deps: ClientDeps) -> None:
    """``cap`` on an unknown ``service`` also fails fast."""
    f = Factory(client_deps)
    f.register("mock", MockClient)
    with pytest.raises(KeyError, match="Unknown service 'nope'"):
        f.cap("nope")
