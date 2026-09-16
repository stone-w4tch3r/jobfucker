"""CLI tests for the ``jobfucker search`` preview command.

Runs the real typer CLI over the real mock client (offline canned vacancies)
with only the ``AppServices.open`` seam pointed at the shared in-memory
storage — the same harness the other stage tests use. Proves: text/json/yaml
rendering, the per-vacancy DB status column (new / stored / soft-deleted),
query+params overrides (file, stdin, inline JSON) with header echo, local
rejection of invalid params, and that the command is read-only (vacancy count
unchanged).
"""

from __future__ import annotations

import json as json_module
import logging
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest
import yaml as yaml_module
from rusty_results.prelude import Ok, Result
from typer.testing import CliRunner
from typer.testing import Result as CliResult

from jobfucker.app.pipeline_service import PipelineService
from jobfucker.app.services import AppServices
from jobfucker.app.vacancy_documents import VacancyDocumentService
from jobfucker.bootstrap import TerminalAuthInteraction
from jobfucker.cli import app
from jobfucker.clients.base import ClientCredentials, ClientDeps
from jobfucker.clients.factory import Factory
from jobfucker.clients.mock.client import MockClient
from jobfucker.clients.mock.params import MockSearchEntry, MockSearchParams, MockServiceConfig
from jobfucker.storage.db import Storage
from jobfucker.storage.vacancy_documents import SqlAlchemyVacancyDocumentStore
from jobfucker.testing.step_runner import async_run
from test.pipeline_helpers import mock_vacancies
from test.storage.builders import build_vacancy

runner = CliRunner()


def _json_document(
    raw: str,
) -> dict[str, object]:  # lint-ignore[raw-dict]: JSON payload  # lint-ignore[restricted-object]: JSON payload
    """Parse the command's ``--format json`` output (typed boundary over ``json.loads``)."""
    document: dict[str, object] = json_module.loads(raw)  # type: ignore[reportAny]  # rationale: json.loads returns Any; narrowed by the assert below  # lint-ignore[restricted-object]: parsed JSON payload boundary
    assert isinstance(document, dict)
    return document


def _status_by_external_id(
    document: dict[str, object],  # lint-ignore[restricted-object]: JSON payload  # lint-ignore[raw-dict]: JSON payload
) -> dict[str, str]:  # lint-ignore[raw-dict]: statuses map
    """Map the document's items onto ``{external_id: db_status}`` (typed at this seam)."""
    items: object = document["items"]  # lint-ignore[restricted-object]: parsed JSON/YAML payload boundary
    assert isinstance(items, list)
    statuses: dict[str, str] = {}
    for item in items:  # type: ignore[reportUnknownVariableType]  # rationale: parsed JSON list; fields asserted below
        external_id: object = item["external_id"]  # type: ignore[reportUnknownVariableType, reportAttributeAccessIssue]  # rationale: same parsed payload  # lint-ignore[restricted-object]: parsed JSON/YAML payload boundary
        status: object = item["db_status"]  # type: ignore[reportUnknownVariableType, reportAttributeAccessIssue]  # rationale: same parsed payload  # lint-ignore[restricted-object]: parsed JSON/YAML payload boundary
        assert isinstance(external_id, str) and isinstance(status, str)
        statuses[external_id] = status
    return statuses


# The real bootstrap fail-fasts without a captcha handler; these flags select
# the terminal handler offline (never invoked: the mock has no captcha here).
_CAPTCHA_FLAGS = ("--no-captcha-ai", "--use-sixel")


def _invoke_search(*args: str, stdin_text: str | None = None) -> CliResult:
    """Invoke ``search`` with the offline captcha flags (optional stdin payload)."""
    return runner.invoke(app, ["search", *_CAPTCHA_FLAGS, *args], input=stdin_text)


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:  # type: ignore[reportUnusedFunction]  # rationale: autouse pytest fixture, applied by the pytest plugin
    """Restore the root logger around every test (the CLI callback mutates it)."""
    root = logging.getLogger()
    original_handlers = root.handlers.copy()
    original_level = root.level
    try:
        yield
    finally:
        for handler in root.handlers:
            if handler not in original_handlers:
                handler.close()
        root.handlers[:] = original_handlers
        root.setLevel(original_level)


async def _captcha_stub(_image: bytes) -> Result[str, str]:
    return Ok("stub-captcha")


def _stub_open(storage: Storage) -> Callable[..., Awaitable[Result[AppServices, str]]]:
    """A fake ``AppServices.open`` over the in-memory ``storage`` + a real mock factory."""

    async def fake(*, storage_override: Storage | None = None) -> Result[AppServices, str]:
        del storage_override
        auth = TerminalAuthInteraction()
        deps = ClientDeps(
            service="mock",
            profile_id=None,
            data_dir=Path("jobfucker-search-data"),
            credentials=ClientCredentials(login="test-login", password="test-password"),
            auth_interaction=auth,
            captcha_handler=_captcha_stub,
        )
        factory = Factory(
            deps,
            section=MockServiceConfig(
                resume_id="mock-resume-1",
                searches=(
                    MockSearchEntry(
                        query="python",
                        filter=MockSearchParams(
                            area=(1,), schedule=("fullDay",), experience="between1And3", only_with_salary=True
                        ),
                        vacancies=mock_vacancies(),
                    ),
                ),
            ),
        )
        factory.register("mock", MockClient)
        documents = VacancyDocumentService.create(SqlAlchemyVacancyDocumentStore(storage.session_factory))
        assert documents.is_ok
        return Ok(
            AppServices(
                storage=storage,
                factory=factory,
                pipelines=PipelineService(storage, factory),
                vacancies=documents.unwrap(),
            )
        )

    return fake


@pytest.fixture
def initialized_pipeline(monkeypatch: pytest.MonkeyPatch, storage: Storage, mock_pipeline_yaml: Path) -> int:
    """A stored mock pipeline (the first identity in the fresh DB gets id 1)."""
    monkeypatch.setattr("jobfucker.cli.AppServices.open", _stub_open(storage))
    result = runner.invoke(app, ["init", "--config", str(mock_pipeline_yaml)])
    assert result.exit_code == 0, result.output
    return 1


def _count(storage: Storage, pipeline_id: int) -> int:
    """The stored vacancy count in any state (the read-only proof)."""
    return len(async_run(storage.vacancies.list_states_by_pipeline(pipeline_id)))


def test_search_text_output_shows_header_rows_and_new_status(initialized_pipeline: int, storage: Storage) -> None:
    result = _invoke_search("--pipeline-id", str(initialized_pipeline))
    assert result.exit_code == 0, result.output
    assert "pipeline: mock-demo (#1)" in result.output
    assert "query:    python  (search 0)" in result.output
    assert "params:   pipeline filters (no override)" in result.output
    assert "web:" not in result.output  # the mock has no web search URL
    assert "db" in result.output and "published" in result.output
    assert "  new  " in result.output  # nothing stored yet: every row is new
    assert "Python" in result.output  # the canned titles come through
    assert "3 vacancies, 1 page(s) scanned." in result.output


async def _mark_soft_deleted(storage: Storage, external_id: str) -> None:
    """Soft-delete directly: it is a manual lifecycle signal set outside fetch/upsert."""
    from sqlalchemy import text

    async with storage.engine.begin() as conn:
        await conn.execute(
            text("UPDATE vacancies SET soft_deleted_at = '2026-08-31 00:00:00' WHERE external_id = :external_id"),
            {"external_id": external_id},
        )


def test_search_db_status_reflects_stored_rows(initialized_pipeline: int, storage: Storage) -> None:
    async_run(storage.vacancies.upsert(build_vacancy(pipeline_id=initialized_pipeline, external_id="mock-1", score=4)))
    async_run(
        storage.vacancies.upsert(
            build_vacancy(pipeline_id=initialized_pipeline, external_id="mock-2", apply_status="applied")
        )
    )
    # mock-3 is soft-deleted: it must read as `deleted`, never as `new`
    # (the all-states read proves the soft-deleted row is visible).
    async_run(storage.vacancies.upsert(build_vacancy(pipeline_id=initialized_pipeline, external_id="mock-3")))
    async_run(_mark_soft_deleted(storage, "mock-3"))

    result = _invoke_search("--pipeline-id", str(initialized_pipeline), "--format", "json")
    assert result.exit_code == 0, result.output
    statuses = _status_by_external_id(_json_document(result.output))
    assert statuses["mock-1"] == "scored"
    assert statuses["mock-2"] == "applied"
    assert statuses["mock-3"] == "deleted"


def test_search_json_output_parses_with_db_status(initialized_pipeline: int) -> None:
    result = _invoke_search("--pipeline-id", str(initialized_pipeline), "--format", "json")
    assert result.exit_code == 0, result.output
    document = _json_document(result.output)
    assert document["query"] == "python"
    assert document["query_source"] == "search 0"
    assert document["found"] == 3
    assert document["ui_url"] is None
    statuses = _status_by_external_id(document)
    assert set(statuses.values()) == {"new"}
    items: object = document["items"]  # lint-ignore[restricted-object]: parsed JSON payload
    assert isinstance(items, list) and len(items) == 3  # type: ignore[reportUnknownArgumentType]  # rationale: parsed JSON payload
    first: object = items[0]  # type: ignore[reportUnknownVariableType]  # rationale: parsed JSON payload; key set asserted below  # lint-ignore[restricted-object]: parsed JSON/YAML payload boundary
    assert isinstance(first, dict)
    keys: object = first.keys()  # type: ignore[reportUnknownVariableType, reportAttributeAccessIssue]  # rationale: parsed JSON payload; key names asserted below  # lint-ignore[restricted-object]: parsed JSON/YAML payload boundary
    assert {"title", "url", "company", "salary", "area", "published_at"} <= set(keys)  # type: ignore[reportUnknownArgumentType]  # rationale: parsed JSON payload


def test_search_yaml_output_parses(initialized_pipeline: int) -> None:
    result = _invoke_search("--pipeline-id", str(initialized_pipeline), "--format", "yaml")
    assert result.exit_code == 0, result.output
    loaded: object = yaml_module.safe_load(result.output)  # type: ignore[reportAny]  # rationale: yaml.safe_load returns Any; narrowed by the assert below  # lint-ignore[restricted-object]: parsed JSON/YAML payload boundary
    assert isinstance(loaded, dict)
    document: dict[str, object] = loaded  # type: ignore[reportUnknownVariableType]  # rationale: parsed YAML payload  # lint-ignore[raw-dict]: test-side parse  # lint-ignore[restricted-object]: parsed YAML payload boundary
    assert document["query"] == "python"
    items: object = document["items"]  # lint-ignore[restricted-object]: parsed JSON payload
    assert isinstance(items, list) and len(items) == 3  # type: ignore[reportUnknownArgumentType]  # rationale: parsed JSON payload


def test_search_query_override_echoes(initialized_pipeline: int) -> None:
    result = _invoke_search("--pipeline-id", str(initialized_pipeline), "--query", "(python OR fastapi)")
    assert result.exit_code == 0, result.output
    assert "query:    (python OR fastapi)  (--query)" in result.output


def test_search_params_inline_json_override(initialized_pipeline: int) -> None:
    result = _invoke_search(
        "--pipeline-id",
        str(initialized_pipeline),
        "--params",
        '{"area": [1], "schedule": [], "experience": null, "only_with_salary": false}',
    )
    assert result.exit_code == 0, result.output
    assert "params:   inline" in result.output


def test_search_params_file_override(tmp_path: Path, initialized_pipeline: int) -> None:
    params_file = tmp_path / "try.yaml"
    params_file.write_text("area: [40]\nschedule: []\nexperience: null\nonly_with_salary: false\n", encoding="utf-8")
    result = _invoke_search("--pipeline-id", str(initialized_pipeline), "--params", str(params_file))
    assert result.exit_code == 0, result.output
    assert f"params:   {params_file}" in result.output


def test_search_params_file_override_with_tilde(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, initialized_pipeline: int
) -> None:
    """A ``~/``-style ``--params`` path expands against the home dir; the header echo keeps the raw value."""
    home = tmp_path / "home"
    params_file = home / "try.yaml"
    params_file.parent.mkdir(parents=True)
    params_file.write_text("area: [40]\nschedule: []\nexperience: null\nonly_with_salary: false\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    result = _invoke_search("--pipeline-id", str(initialized_pipeline), "--params", "~/try.yaml")
    assert result.exit_code == 0, result.output
    assert "params:   ~/try.yaml" in result.output


def test_search_params_stdin_override(initialized_pipeline: int) -> None:
    result = _invoke_search(
        "--pipeline-id",
        str(initialized_pipeline),
        "--params",
        "-",
        stdin_text="area: [1]\nschedule: []\nexperience: null\nonly_with_salary: true\n",
    )
    assert result.exit_code == 0, result.output
    assert "params:   stdin" in result.output


def test_search_rejects_invalid_params(initialized_pipeline: int, storage: Storage) -> None:
    before = _count(storage, initialized_pipeline)
    result = _invoke_search("--pipeline-id", str(initialized_pipeline), "--params", '{"areas": [1]}')
    assert result.exit_code == 1
    assert "areas" in result.output  # the pydantic error names the unknown field
    assert _count(storage, initialized_pipeline) == before  # nothing written


def test_search_is_read_only(initialized_pipeline: int, storage: Storage) -> None:
    before = _count(storage, initialized_pipeline)
    result = _invoke_search("--pipeline-id", str(initialized_pipeline))
    assert result.exit_code == 0, result.output
    assert _count(storage, initialized_pipeline) == before  # no upserts, no audit rows


def test_search_missing_params_file_fails_readably(initialized_pipeline: int) -> None:
    result = _invoke_search("--pipeline-id", str(initialized_pipeline), "--params", "/nonexistent/try.yaml")
    assert result.exit_code == 1
    assert "Cannot read --params file" in result.output


def test_search_non_mapping_params_fail(initialized_pipeline: int) -> None:
    result = _invoke_search("--pipeline-id", str(initialized_pipeline), "--params", "[1, 2]")
    assert result.exit_code == 1
    assert "mapping" in result.output


def test_search_missing_pipeline_fails(initialized_pipeline: int) -> None:
    result = _invoke_search("--pipeline-id", "999")
    assert result.exit_code == 1


def test_search_window_flags_map_onto_listing_positions(initialized_pipeline: int) -> None:
    """--first-page/--page-size/--take-pages select the exact short-item window."""
    result = _invoke_search(
        "--pipeline-id",
        str(initialized_pipeline),
        "--page-size",
        "50",
        "--take-pages",
        "1",
        "--format",
        "json",
    )
    assert result.exit_code == 0, result.output
    document = _json_document(result.output)
    items: object = document["items"]  # lint-ignore[restricted-object]: parsed JSON payload
    assert isinstance(items, list) and len(items) == 3  # type: ignore[reportUnknownArgumentType]  # rationale: parsed JSON payload
    assert document["pages_scanned"] == 1


def test_search_rejects_invalid_page_size_locally(initialized_pipeline: int) -> None:
    """The fetch plan's validation matrix runs before any board call."""
    result = _invoke_search("--pipeline-id", str(initialized_pipeline), "--page-size", "33")
    assert result.exit_code == 1
    assert "20, 50, 100" in result.output
