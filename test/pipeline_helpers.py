"""Shared helpers for the Phase 5B stage / engine / integration suites.

Builds a validated :class:`PipelineConfig` (with the mock section), stores a
:class:`Pipeline` identity + its :class:`PipelineSnapshot`, and provides a
scripted :class:`AiClient` stub, so the fetch / apply / engine / integration
tests exercise real code paths through real :class:`Storage` and a real
registered client — without any network or a live board.

Post-snapshot rewrite, :func:`create_pipeline` builds an identity row AND its
head snapshot (and points ``current_snapshot_id`` at it), because a stage run
stamps the snapshot id onto every row it writes.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import yaml
from pydantic import TypeAdapter
from rusty_results.prelude import Ok, Result

from jobfucker.ai import ScoreError, ScoreResult
from jobfucker.app.mapping import new_identity, to_pipeline_snapshot
from jobfucker.clients.base import ClientDeps
from jobfucker.clients.factory import Factory
from jobfucker.clients.mock.client import MockClient
from jobfucker.clients.mock.params import (
    MockBehaviorConfig,
    MockSearchEntry,
    MockSearchParams,
    MockServiceConfig,
    MockVacancyParams,
)
from jobfucker.config import (
    ApplyConfig,
    AuthConfig,
    LimitsConfig,
    OpenAIConfig,
    PipelineConfig,
    ResumeConfig,
    ScoringConfig,
)
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, PipelineSnapshot

# Mock's fixed service resume id (reported by get_resumes / the section).
# Public on purpose: shared by tests across areas (test/stages, test/engine...).
MOCK_RESUME_ID = "mock-resume-1"
MOCK_FILTER = MockSearchParams(
    area=(1,),
    schedule=("fullDay",),
    experience="between1And3",
    only_with_salary=True,
)

# Canonical canned vacancies for the mock section — a test fixture, not code.
# test/clients/mock/test_mock_client.py reads the same file; edit it there, not
# in any Python builder. The hermetic CLI config test/fixtures/pipeline.mock.yaml.j2
# mirrors the same three entries inline.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
_MOCK_VACANCIES_FILE = _FIXTURES_DIR / "mock_vacancies.yaml"
_VACANCIES_ADAPTER = TypeAdapter(list[MockVacancyParams])


def mock_vacancies() -> list[MockVacancyParams]:
    """The canonical canned mock vacancies, loaded from ``test/fixtures/mock_vacancies.yaml``.

    The mock's search data lives in the fixture, not inlined in the section
    model or the client; builders that produce a mock section seed it straight
    from here so stage/engine/integration tests share the same three entries the
    mock-client unit tests assert.
    """
    raw = yaml.safe_load(_MOCK_VACANCIES_FILE.read_text(encoding="utf-8"))  # type: ignore[reportAny]  # rationale: PyYAML returns Any; the TypeAdapter re-validates the shape at this boundary
    return _VACANCIES_ADAPTER.validate_python(raw)


class ScriptedAi:
    """An :class:`AiClient` stub returning scripted results in call order.

    Defaults to always-succeed (score 4 + a fixed cover letter), so tests only
    override the calls they care about. Async per the migrated contract.
    """

    def __init__(
        self,
        scores: list[Result[ScoreResult, ScoreError]] | None = None,
        letters: list[Result[str, str]] | None = None,
    ) -> None:
        self._scores = list(scores) if scores is not None else []
        self._letters = list(letters) if letters is not None else []

    async def score(self, prompt: str) -> Result[ScoreResult, ScoreError]:
        """Score, returning the next scripted result or a default success."""
        del prompt
        if self._scores:
            return self._scores.pop(0)
        return Ok(ScoreResult(fit_score=4, comment="scripted good match"))

    async def generate_cover_letter(self, prompt: str) -> Result[str, str]:
        """Generate a cover letter, returning the next scripted text or default."""
        del prompt
        if self._letters:
            return self._letters.pop(0)
        return Ok("A generated cover letter.")


def mock_search_entry(
    vacancies: list[MockVacancyParams] | None = None,
    *,
    query: str = "python",
    filter_: MockSearchParams | None = None,
) -> MockSearchEntry:
    """One default mock pool entry (query ``"python"``, ``MOCK_FILTER``, canned vacancies)."""
    return MockSearchEntry(
        query=query, filter=filter_ if filter_ is not None else MOCK_FILTER, vacancies=vacancies or []
    )


def make_mock_section(
    vacancies: list[MockVacancyParams],
    *,
    query: str = "python",
    behavior: MockBehaviorConfig | None = None,
) -> MockServiceConfig:
    """A mock section with a single-entry pool over the given canned vacancies."""
    return MockServiceConfig(
        resume_id=MOCK_RESUME_ID,
        searches=(mock_search_entry(vacancies, query=query),),
        behavior=behavior,
    )


def build_pipeline_config(
    *,
    daily_apply_limit: int = 50,
    min_required_score: int = 3,
    behavior: MockBehaviorConfig | None = None,
    name: str = "test-pipeline",
    login: str = "",
) -> PipelineConfig:
    """A validated :class:`PipelineConfig` targeting the mock client.

    The scoring/apply prompts are trivial Jinja bodies (no frontmatter); the
    stub AI ignores content, so only their presence matters. ``behavior`` is
    forwarded to the mock service section to program exercise outcomes.
    ``login``/``name`` override the auth account / user label (defaults leave
    ``login`` empty, matching the loader's file-driven auth).
    """
    config = PipelineConfig(
        name=name,
        description="test pipeline",
        service="mock",
        auth=AuthConfig(login_file=Path("secrets/login.txt"), password_file=Path("secrets/password.txt"), login=login),
        resume=ResumeConfig(path=Path("cvs/CV.md"), contents="resume contents"),
        openai=OpenAIConfig(
            model="gpt-test",
            base_url="https://api.example.com/v1",
            api_key="test-key",
        ),
        openai_captcha=None,
        scoring=ScoringConfig(min_required_score=min_required_score, scoring_prompt="Score {{ vacancy_formatted }}"),
        apply=ApplyConfig(apply_prompt="Apply for {{ vacancy_formatted }}"),
        limits=LimitsConfig(daily_apply_limit=daily_apply_limit),
    )
    config.set_service_section(make_mock_section(mock_vacancies(), behavior=behavior))
    return config


async def create_pipeline(
    storage: Storage,
    *,
    name: str = "engine-test",
    min_required_score: int = 3,
    daily_apply_limit: int = 50,
    login: str = "login@example.com",
) -> Pipeline:
    """Create + store a :class:`Pipeline` identity with the given thresholds.

    Also stores the head :class:`PipelineSnapshot` (built from a matching
    config) and points ``current_snapshot_id`` at it, so a stage run has a
    snapshot to stamp onto the rows it writes.
    """
    config = build_pipeline_config(
        name=name,
        daily_apply_limit=daily_apply_limit,
        min_required_score=min_required_score,
        login=login,
    )
    identity, _snapshot = await store_pipeline(storage, config)
    return identity


def build_pipeline_snapshot(
    config: PipelineConfig,
    *,
    pipeline_id: int,
    snapshot_no: int = 1,
    source: str = "from_file",
    note: str | None = None,
) -> PipelineSnapshot:
    """Build a persistable :class:`PipelineSnapshot` DTO for ``config``.

    Identity/bookkeeping fields (``id``/``pipeline_id``/``snapshot_no``/
    ``source``/``note``) are filled in by the caller (here the test); the config
    content columns come from ``to_pipeline_snapshot``.
    """
    base = to_pipeline_snapshot(config)
    return replace(
        base,
        pipeline_id=pipeline_id,
        snapshot_no=snapshot_no,
        source=source,
        note=note,
    )


async def store_pipeline(storage: Storage, config: PipelineConfig) -> tuple[Pipeline, PipelineSnapshot]:
    """Persist a fresh identity + head snapshot for ``config`` and point at it.

    Mirrors the ``init`` path (identity + snapshot #1 + ``current_snapshot_id``)
    without the cap/section validation the service applies. Returns the stored
    identity and its head snapshot.
    """
    identity = await storage.pipelines.create(new_identity(config.name, config.description))
    snapshot = await storage.snapshots.create(build_pipeline_snapshot(config, pipeline_id=identity.id))
    await storage.pipelines.set_current_snapshot(identity.id, snapshot.id)
    return identity, snapshot


async def snapshot_for(storage: Storage, pipeline: Pipeline) -> PipelineSnapshot:
    """Return the identity's current head snapshot (or fail the test if absent)."""
    snapshot = await storage.snapshots.current(pipeline.id)
    if snapshot is None:
        raise AssertionError(f"pipeline {pipeline.id} has no current snapshot")
    return snapshot


def make_factory(client_deps: ClientDeps, *, section: MockServiceConfig | None = None) -> Factory:
    """A factory with the mock client registered (optionally with a section).

    When ``section`` is not given, the factory is bound to the default mock
    section (``resume_id=MOCK_RESUME_ID``, the default single-entry pool) so
    existing no-arg call sites keep working. ``section.behavior`` programs the
    registered mock's outcomes, so the real factory path yields a scriptable
    client (e.g. one that returns ``LimitExceededError``).
    """
    if section is None:
        section = make_mock_section(mock_vacancies())
    factory = Factory(client_deps, section=section)
    factory.register("mock", MockClient)
    return factory


def make_bulk_section(count: int) -> MockServiceConfig:
    """A mock section with ``count`` canned vacancies (ids ``bulk-0..bulk-{count-1}``).

    Shared by the fetch-slice tests (test/stages, test/engine): a listing long
    enough to exercise multi-page slices, page trimming, and early listing end.
    """
    return make_mock_section(
        [
            MockVacancyParams(
                external_id=f"bulk-{i}",
                title=f"Bulk vacancy {i}",
                url=f"https://mock.example/vacancies/bulk-{i}",
                description="A sufficiently long plaintext description for the bulk listing fixture.",
            )
            for i in range(count)
        ]
    )
