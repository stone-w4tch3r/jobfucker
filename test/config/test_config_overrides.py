"""Unit coverage for :func:`jobfucker.config.with_overrides` (search-preview overrides).

Pins the override contract: one pool entry selected by ``search_index``, query
replacement, full-section revalidation with the entry's ``filter`` swapped
(siblings + other entries + alias fields survive), strict local rejection of
unknown keys / bad enums / cross-field violations, out-of-range index, and the
no-filter-board fail fast.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from jobfucker.clients.hh.config import (
    Currency,
    HHFilterConfig,
    HHOrderingFilters,
    HHSalaryFilters,
    HHSearchEntry,
    HHServiceConfig,
    SortBy,
)
from jobfucker.clients.mock.params import (
    MockApplyBehaviorConfig,
    MockBehaviorConfig,
    MockSearchEntry,
    MockSearchParams,
    MockServiceConfig,
)
from jobfucker.config import (
    ApplyConfig,
    AuthConfig,
    LimitsConfig,
    OpenAIConfig,
    PipelineConfig,
    ResumeConfig,
    ScoringConfig,
    ServiceConfigSection,
    with_overrides,
)

# An inert path placeholder: overrides never touch referenced files (same
# pattern as the DB reconstruction path in ``reconstruct_pipeline_config``).
_UNUSED_PATH = Path()


def _config(service: str, section: ServiceConfigSection) -> PipelineConfig:
    """A minimal valid ``PipelineConfig`` carrying the given section."""
    config = PipelineConfig(
        name="override-demo",
        description="",
        service=service,
        auth=AuthConfig(login_file=_UNUSED_PATH, password_file=_UNUSED_PATH, login="login", password="password"),
        resume=ResumeConfig(path=_UNUSED_PATH, contents="resume"),
        openai=OpenAIConfig(model="gpt-test", base_url="https://api.openai.com/v1", api_key="key"),
        scoring=ScoringConfig(min_required_score=3, scoring_prompt="score"),
        apply=ApplyConfig(apply_prompt="apply"),
        limits=LimitsConfig(daily_apply_limit=5),
    )
    config.set_service_section(section)
    return config


def _mock_section(*, with_behavior: bool = False) -> MockServiceConfig:
    """A mock section with two entries; optionally seeded with a behavior block."""
    return MockServiceConfig(
        resume_id="mock-resume-1",
        searches=(
            MockSearchEntry(
                query="python",
                filter=MockSearchParams(area=(1,), schedule=(), experience=None, only_with_salary=True),
            ),
            MockSearchEntry(query="fastapi"),
        ),
        behavior=MockBehaviorConfig(default_apply=MockApplyBehaviorConfig(outcome="applied"))
        if with_behavior
        else None,
    )


def test_with_overrides_noop_returns_the_same_config() -> None:
    config = _config("mock", _mock_section())
    result = with_overrides(config, search_index=0)
    assert result.is_ok
    assert result.unwrap() is config


def test_query_override_lands_and_keeps_the_section() -> None:
    config = _config("mock", _mock_section())
    result = with_overrides(config, search_index=0, query="(python OR fastapi)")
    assert result.is_ok
    overridden = result.unwrap()
    assert overridden.service_section.searches[0].query == "(python OR fastapi)"
    assert overridden.service_section.searches[1].query == "fastapi"  # sibling entry untouched
    assert overridden.service_section.resume_id == "mock-resume-1"


def test_query_override_on_second_entry() -> None:
    config = _config("mock", _mock_section())
    result = with_overrides(config, search_index=1, query="django")
    assert result.is_ok
    overridden = result.unwrap()
    assert overridden.service_section.searches[1].query == "django"
    assert overridden.service_section.searches[0].query == "python"  # first entry untouched


def test_search_index_out_of_range_is_rejected() -> None:
    config = _config("mock", _mock_section())
    result = with_overrides(config, search_index=2, query="x")
    assert result.is_err
    message = result.unwrap_err()
    assert "2 search(es)" in message
    assert "0 to 1" in message


def test_filter_override_replaces_filter_and_keeps_siblings() -> None:
    config = _config("mock", _mock_section())
    result = with_overrides(
        config,
        search_index=0,
        filter_data={"area": [40], "schedule": ["remote"], "experience": "between3And6", "only_with_salary": True},
    )
    assert result.is_ok
    section = result.unwrap().service_section
    assert isinstance(section, MockServiceConfig)
    assert section.searches[0].filter.area == (40,)
    assert section.searches[0].filter.experience == "between3And6"
    assert section.searches[0].filter.only_with_salary is True  # untouched sibling field
    assert section.searches[1].filter.area == ()  # sibling entry's default filter untouched
    assert section.resume_id == "mock-resume-1"  # untouched section fields


def test_filter_override_full_section_revalidation_keeps_behavior() -> None:
    config = _config("mock", _mock_section(with_behavior=True))
    result = with_overrides(
        config, search_index=0, filter_data={"area": [], "schedule": (), "experience": None, "only_with_salary": False}
    )
    assert result.is_ok
    section = result.unwrap().service_section
    assert isinstance(section, MockServiceConfig)
    assert section.behavior is not None  # the behavior block survived the revalidation
    assert section.behavior.default_apply is not None


def test_filter_override_unknown_key_is_rejected_locally() -> None:
    config = _config("mock", _mock_section())
    result = with_overrides(
        config, search_index=0, filter_data={"areas": [1]}
    )  # typo: field is `area`; full doc required
    assert result.is_err
    assert "areas" in result.unwrap_err()


def test_filter_override_bad_enum_value_is_rejected_locally() -> None:
    config = _config("mock", _mock_section())
    result = with_overrides(config, search_index=0, filter_data={"area": (1,), "schedule": (), "experience": "wizard"})
    assert result.is_err


def test_hh_filter_override_alias_roundtrip() -> None:
    config = _config(
        "hh",
        HHServiceConfig(
            resume_id="hh-resume-1",
            searches=(
                HHSearchEntry(
                    query="python",
                    filter=HHFilterConfig(salary=HHSalaryFilters(from_=250000, currency=Currency.RUR)),
                ),
            ),
        ),
    )
    result = with_overrides(
        config, search_index=0, filter_data={"salary": {"from": 300000, "currency": "usd"}}
    )  # full HH filter doc
    assert result.is_ok
    section = result.unwrap().service_section
    assert isinstance(section, HHServiceConfig)
    # The alias roundtrip: authored ``from`` lands on ``from_`` (the same
    # roundtrip the DB persistence path uses).
    assert section.searches[0].filter.salary.from_ == 300000
    assert section.searches[0].filter.salary.currency is not None
    assert section.searches[0].filter.salary.currency == Currency.USD


def test_hh_filter_override_violates_distance_sort_rule() -> None:
    config = _config(
        "hh",
        HHServiceConfig(
            resume_id="hh-resume-1",
            searches=(
                HHSearchEntry(
                    query="python",
                    filter=HHFilterConfig(ordering=HHOrderingFilters(sort_results_by=SortBy.RELEVANCE)),
                ),
            ),
        ),
    )
    result = with_overrides(config, search_index=0, filter_data={"ordering": {"sort_results_by": "distance"}})
    assert result.is_err
    assert "distance" in result.unwrap_err()


def test_section_without_filter_field_cannot_be_overridden() -> None:
    class _FilterlessEntry(BaseModel):
        """A BaseModel entry shape with no ``filter`` field (only ``query``)."""

        query: str = "fake query"

    class _FilterlessSection(BaseModel):
        """A BaseModel section shape with only ``resume_id`` + ``searches``."""

        resume_id: str = "fake-resume-1"
        searches: tuple[_FilterlessEntry, ...] = (_FilterlessEntry(),)

    section = _FilterlessSection()
    config = _config("fake", section)  # type: ignore[arg-type]  # rationale: unregistered double section by design
    result = with_overrides(config, search_index=0, filter_data={"anything": 1})
    assert result.is_err
    assert "no filter to override" in result.unwrap_err()


def test_non_pydantic_entry_is_a_programming_error() -> None:
    """Every registered entry is a pydantic model; anything else is a bug (fail fast)."""
    from test.fakes.fake_client import FakeServiceConfig

    config = _config("fake", _mock_section())  # a valid pydantic config
    config.set_service_section(FakeServiceConfig(resume_id="fake-resume-1"))  # type: ignore[arg-type]  # rationale: unregistered dataclass double, deliberately outside the protocol
    with pytest.raises(TypeError, match="pydantic BaseModel"):
        with_overrides(config, search_index=0, filter_data={"anything": 1})
