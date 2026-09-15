"""Phase 2 (task 2.4): conformance of the canonical in-memory fake.

``test/fakes/fake_client.py`` is the **living conformance check** for the frozen
client contract: a contract change that drops/renames/retypes a ``Client``
member without mirroring it there breaks this module (the smoke signal per
client-contract.md "Stability & governance"). We assert protocol membership AND
that the fake exposes every required method by hand, so the smoke signal cannot
be silently defeated.

The fake is constructed like any client — from a ``(deps, section)`` pair
(``ClientDeps`` + a ``FakeServiceConfig``) — matching the non-generic ``Client``
protocol, which carries no ``SearchParams``.
"""

from __future__ import annotations

from dataclasses import fields

from jobfucker.clients.base import Client, ClientDeps, ServiceVacancyId, Vacancy
from test.fakes.fake_client import FakeClient, FakeServiceConfig

# The fake's declared per-auth daily cap (asserted in conformance).
_FAKE_CAP = 200

_REQUIRED_CLIENT_METHODS = (
    "authorize",
    "search_vacancies",
    "list_vacancies",
    "get_resumes",
    "apply_to_vacancy",
    "aclose",
)


def _make_fake(client_deps: ClientDeps) -> FakeClient:
    """Construct the fake exactly like a real client — ``(deps, section)`` pair."""
    return FakeClient(
        client_deps,
        FakeServiceConfig(resume_id="fake-resume-1"),  # type: ignore[arg-type]  # rationale: unregistered dataclass double, deliberately outside the protocol
    )


async def test_fake_implements_client_protocol(client_deps: ClientDeps) -> None:
    """The fake satisfies the ``Client`` protocol at runtime."""
    fake = _make_fake(client_deps)
    assert isinstance(fake, Client)
    assert fake.service == "fake"
    assert fake.service_info.per_auth_daily_cap == _FAKE_CAP


async def test_fake_exposes_every_required_client_method(client_deps: ClientDeps) -> None:
    """Smoke signal: a dropped ``Client`` member on the fake fails loudly."""
    fake = _make_fake(client_deps)
    for name in _REQUIRED_CLIENT_METHODS:
        assert hasattr(fake, name), f"FakeClient is missing Client member {name!r}"


async def test_fake_search_accepts_query_only(client_deps: ClientDeps) -> None:
    """The fake's ``search_vacancies`` takes a query (defaults cover the rest)."""
    fake = _make_fake(client_deps)
    result = await fake.search_vacancies(0)
    assert result.is_ok
    assert len(result.unwrap().items) > 0


async def test_fake_search_exclude_filters_stored_ids(client_deps: ClientDeps) -> None:
    """The optional ``exclude`` drops stored ids from the slice (contract parity)."""
    fake = _make_fake(client_deps)
    result = await fake.search_vacancies(0, exclude=frozenset({ServiceVacancyId("fake-1")}))
    assert result.is_ok
    assert [v.external_id for v in result.unwrap().items] == [ServiceVacancyId("fake-2")]


async def test_fake_list_vacancies_returns_short_items_without_exclude(client_deps: ClientDeps) -> None:
    """``list_vacancies`` returns short items with envelope metadata and no ``exclude`` arg."""
    fake = _make_fake(client_deps)
    result = await fake.list_vacancies(0)
    assert result.is_ok
    listing = result.unwrap()
    assert [v.external_id for v in listing.items] == [ServiceVacancyId("fake-1"), ServiceVacancyId("fake-2")]
    assert listing.found == 2
    assert listing.ui_url is None  # the fake has no web page
    assert listing.offset == 0


def test_vacancy_contract_has_no_archived_marker() -> None:
    """The board ``archived`` marker is dropped from the contract entirely.

    ``soft_deleted_at`` is the only lifecycle signal and is set manually,
    never by fetch; the contract ``Vacancy`` no longer carries a board
    archived/closed flag.
    """
    field_names = {field.name for field in fields(Vacancy)}
    assert "archived" not in field_names
    assert {"external_id", "title", "url", "company", "description", "key_skills", "salary"} <= field_names
