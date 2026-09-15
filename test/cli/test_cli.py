"""CLI tests — distinct per-stage commands from ``--pipeline-id``.

Uses typer's ``CliRunner`` against the real CLI, exercising:

- ``-h`` lists all commands;
- ``init``/``update``/``status``/``pipelines-list`` against a real (isolated
  ``DATA_DIR``) migrated DB + the first-party mock; ``init``/``update`` carry
  the ``--config`` flag only (captcha flags are rejected);
- the stage/run commands build their engine **from the stored pipeline by id**
  (no fresh ``--config``): the storage-wide ``AppServices.open()`` and
  the engine-rebuild ``build_engine_from_pipeline`` are stubbed so the command
  uses the injected in-memory ``storage`` fixture and a mock-backed engine with
  an injected stub AI (no network);
- top-level ``Err`` (captcha fail-fast) maps to a non-zero exit on the
  client-constructing commands.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from rusty_results.prelude import Err, Ok, Result
from sqlalchemy.ext.asyncio import AsyncEngine
from typer.testing import CliRunner

from jobfucker.ai import AiClient
from jobfucker.app.pipeline_service import PipelineService
from jobfucker.app.services import AppServices
from jobfucker.app.vacancy_documents import VacancyDocumentService
from jobfucker.bootstrap import TerminalAuthInteraction
from jobfucker.cli import app
from jobfucker.clients.base import ClientCredentials, ClientDeps
from jobfucker.clients.factory import Factory
from jobfucker.clients.mock.client import MockClient
from jobfucker.clients.mock.params import (
    MockApplyBehaviorConfig,
    MockBehaviorConfig,
    MockSearchEntry,
    MockSearchParams,
    MockServiceConfig,
)
from jobfucker.engine import Engine
from jobfucker.reporting import Reporter
from jobfucker.storage.db import Storage, create_async_sqlite_engine, storage_from_engine
from jobfucker.storage.dto import Pipeline, PipelineSnapshot
from jobfucker.storage.vacancy_documents import SqlAlchemyVacancyDocumentStore
from jobfucker.testing.step_runner import async_run
from test.pipeline_helpers import ScriptedAi, build_pipeline_config, create_pipeline, mock_vacancies

runner = CliRunner()


@pytest.fixture(autouse=True)
def _restore_root_logger() -> Iterator[None]:  # type: ignore[reportUnusedFunction]  # rationale: autouse pytest fixture, applied by the pytest plugin
    """Restore the root logger around every test.

    The CLI's logging callback mutates the (process-global) root logger —
    adding file/console handlers and lowering the root level. Without a
    restore, those handlers would leak into every later test in the worker
    (including writes to already-closed CliRunner capture streams).
    """
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


def _vacancy_documents(storage: Storage) -> VacancyDocumentService:
    result = VacancyDocumentService.create(SqlAlchemyVacancyDocumentStore(storage.session_factory))
    assert result.is_ok
    return result.unwrap()


async def _captcha_stub(_image: bytes) -> Result[str, str]:
    return Ok("stub-captcha")


def _make_factory(behavior: MockBehaviorConfig | None = None) -> Factory:
    """A real mock-backed :class:`Factory` (mock registered, stub captcha).

    Shared by :func:`_make_engine` and the fake ``AppServices`` so the real
    per-auth cap (200) runs during ``init``/``update``/``run --new-from-config``
    via :func:`validate_cap` inside ``PipelineService``. An optional behavior
    seam scripts mock apply outcomes.
    """
    auth = TerminalAuthInteraction()
    deps = ClientDeps(
        service="mock",
        profile_id=None,
        data_dir=Path("jobfucker-cli-data"),
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
                        area=(1,),
                        schedule=("fullDay",),
                        experience="between1And3",
                        only_with_salary=True,
                    ),
                    # Same canned dataset every other mock-backed test uses
                    # (test/fixtures/mock_vacancies.yaml), so the CLI engine's fetch
                    # finds all three entries.
                    vacancies=mock_vacancies(),
                ),
            ),
            behavior=behavior,
        ),
    )
    factory.register("mock", MockClient)
    return factory


def _make_engine(
    storage: Storage,
    ai: AiClient,
    snapshot_id: int = 0,
    behavior: MockBehaviorConfig | None = None,
    progress: Reporter | None = None,
) -> Engine:
    """A real mock-backed :class:`Engine` (never touches the network).

    Built from :func:`build_pipeline_config` so it carries the mock section and
    the stub AI; the engine resolves its own client/params from its config, so
    it runs against whatever stored pipeline the CLI resolves. The stub engine
    never writes provenance rows the CLI asserts on, so a fixed ``snapshot_id``
    is inert here. ``progress`` forwards the CLI's reporter so live run events
    print in the captured output.
    """
    return Engine(
        storage=storage,
        factory=_make_factory(behavior),
        config=build_pipeline_config(),
        ai=ai,
        snapshot_id=snapshot_id,
        progress=progress,
    )


def _stub_bootstrap(storage: Storage) -> Callable[..., Awaitable[Result[AppServices, str]]]:
    """A fake ``AppServices.open`` returning the injected in-memory ``storage``.

    Mirrors the new seam the stage/run commands rely on (they call
    ``await AppServices.open()``): it returns :class:`AppServices` built
    on the in-memory ``storage`` fixture with a **real** mock-backed factory /
    :class:`PipelineService`, so ``init``/``update``/``run --new-from-config``
    run ``validate_cap`` against the real mock per-auth cap (200).
    """

    async def fake(*, storage_override: Storage | None = None) -> Result[AppServices, str]:
        del storage_override
        factory = _make_factory()
        return Ok(
            AppServices(
                storage=storage,
                factory=factory,
                pipelines=PipelineService(storage, factory),
                vacancies=_vacancy_documents(storage),
            )
        )

    return fake


def _stub_build_engine(
    storage: Storage, ai: AiClient, behavior: MockBehaviorConfig | None = None
) -> Callable[..., Result[Engine, str]]:
    """A fake ``build_engine_from_pipeline`` returning a mock-backed engine.

    Stubs the engine-reconstruction seam used by the new commands (the CLI never
    passes a config through ``AppServices``), so a stage/run by ``--pipeline-id``
    exercises the CLI + real mock engine while avoiding real referenced files.
    """

    def fake(
        storage_arg: Storage,
        pipeline: Pipeline,
        snapshot: PipelineSnapshot,
        *,
        use_sixel: bool = False,
        use_kitty: bool = False,
        no_captcha_ai: bool = False,
        require_captcha: bool = True,
        ai_override: AiClient | None = None,
        progress: Reporter | None = None,
    ) -> Result[Engine, str]:
        del storage_arg, pipeline, snapshot, use_sixel, use_kitty, no_captcha_ai, require_captcha, ai_override
        return Ok(_make_engine(storage, ai, behavior=behavior, progress=progress))

    return fake


def _stub_build_engine_error(message: str) -> Callable[..., Result[Engine, str]]:
    """A fake ``build_engine_from_pipeline`` returning ``Err(message)``.

    Lets a CLI test force the engine-rebuild failure path (e.g. captcha
    fail-fast) and assert the command maps it to a message + non-zero exit.
    """

    def fake(
        storage_arg: Storage,
        pipeline: Pipeline,
        snapshot: PipelineSnapshot,
        *,
        use_sixel: bool = False,
        use_kitty: bool = False,
        no_captcha_ai: bool = False,
        require_captcha: bool = True,
        ai_override: AiClient | None = None,
        progress: Reporter | None = None,
    ) -> Result[Engine, str]:
        del storage_arg, pipeline, snapshot, use_sixel, use_kitty, no_captcha_ai, require_captcha, ai_override, progress
        return Err(message)

    return fake


def _patch_cli(
    monkeypatch: pytest.MonkeyPatch,
    storage: Storage,
    ai: AiClient | None = None,
    behavior: MockBehaviorConfig | None = None,
) -> None:
    """Stub the CLI's service-graph + engine-rebuild seams for stage tests."""
    monkeypatch.setattr("jobfucker.cli.AppServices.open", _stub_bootstrap(storage))
    monkeypatch.setattr(
        "jobfucker.cli.build_engine_from_pipeline", _stub_build_engine(storage, ai or ScriptedAi(), behavior)
    )


# --- help / reachability ----------------------------------------------------
def test_help_lists_all_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in (
        "init",
        "update",
        "run",
        "fetch",
        "search",
        "score",
        "generate",
        "apply",
        "status",
        "pipelines-list",
    ):
        assert name in result.output


# --- init / update / status / pipelines-list (real mock, isolated DB) -------
def test_init_creates_and_lists_pipeline(mock_pipeline_yaml: Path) -> None:
    result = runner.invoke(app, ["init", "--config", str(mock_pipeline_yaml)])
    assert result.exit_code == 0
    assert "created with id" in result.output
    listed = runner.invoke(app, ["pipelines-list"])
    assert listed.exit_code == 0
    assert "mock-demo" in listed.output


def test_init_is_idempotent_get_or_keep(mock_pipeline_yaml: Path) -> None:
    """A second ``init`` with the same config reuses the identity (no-op)."""
    first = runner.invoke(app, ["init", "--config", str(mock_pipeline_yaml)])
    second = runner.invoke(app, ["init", "--config", str(mock_pipeline_yaml)])
    assert first.exit_code == 0 and second.exit_code == 0
    assert "created with id" in first.output
    assert "unchanged" in second.output


def test_init_accepts_only_config(mock_pipeline_yaml: Path) -> None:
    """``init`` rejects captcha flags (it never constructs a client)."""
    ok = runner.invoke(app, ["init", "--config", str(mock_pipeline_yaml)])
    assert ok.exit_code == 0
    bad = runner.invoke(app, ["init", "--config", str(mock_pipeline_yaml), "--use-sixel"])
    assert bad.exit_code != 0
    assert "No such option: --use-sixel" in bad.output


def _changed_mock_pipeline_yaml(mock_pipeline_yaml: Path) -> Path:
    """A second mock yaml with a different query (a real config change).

    Rendering the same Jinja template with an edited ``query`` value gives an
    otherwise-identical config that differs from the stored head snapshot, so an
    ``update`` with it appends snapshot #2 instead of no-opping.
    """
    changed = mock_pipeline_yaml.parent / "pipeline.mock.changed.yaml"
    changed.write_text(
        mock_pipeline_yaml.read_text(encoding="utf-8").replace('query: "python"', 'query: "java"'),
        encoding="utf-8",
    )
    return changed


def test_update_appends_snapshot_and_keeps_id(mock_pipeline_yaml: Path) -> None:
    """``update`` appends a snapshot under the same id (never re-ids / deletes)."""
    first = runner.invoke(app, ["init", "--config", str(mock_pipeline_yaml)])
    second = runner.invoke(app, ["update", "--config", str(_changed_mock_pipeline_yaml(mock_pipeline_yaml))])
    assert first.exit_code == 0 and second.exit_code == 0
    assert "created with id 1" in first.output
    assert "updated (now snapshot #2)" in second.output
    listed = runner.invoke(app, ["pipelines-list"])
    # Exactly one identity line; the id never changed (no soft-delete/re-create).
    assert listed.output.count("\tmock-demo\t") == 1
    assert listed.output.startswith("1\tmock-demo\tmock")


def test_update_noop_prints_unchanged(mock_pipeline_yaml: Path) -> None:
    """An identical ``update`` is a no-op: nothing appended, ``unchanged`` printed."""
    runner.invoke(app, ["init", "--config", str(mock_pipeline_yaml)])
    result = runner.invoke(app, ["update", "--config", str(mock_pipeline_yaml)])
    assert result.exit_code == 0
    assert "unchanged" in result.output
    listed = runner.invoke(app, ["pipelines-list", "--snapshots"])
    assert listed.output.count("#1") == 1
    assert "#2" not in listed.output


def test_update_unknown_pipeline_errors(mock_pipeline_yaml: Path) -> None:
    """``update`` without a stored pipeline of that name fails fast with a hint."""
    result = runner.invoke(app, ["update", "--config", str(mock_pipeline_yaml)])
    assert result.exit_code != 0
    assert "No pipeline named 'mock-demo'; run init first" in result.output


def test_pipelines_list_identities_and_snapshots(mock_pipeline_yaml: Path) -> None:
    """``pipelines-list`` prints one line per identity; ``--snapshots`` the chain."""
    runner.invoke(app, ["init", "--config", str(mock_pipeline_yaml)])
    runner.invoke(app, ["update", "--config", str(_changed_mock_pipeline_yaml(mock_pipeline_yaml))])

    plain = runner.invoke(app, ["pipelines-list"])
    assert plain.exit_code == 0
    # One identity line only — snapshots are NOT sibling entries.
    assert plain.output == "1\tmock-demo\tmock\n"

    nested = runner.invoke(app, ["pipelines-list", "--snapshots"])
    assert nested.exit_code == 0
    assert "1\tmock-demo\tmock" in nested.output
    assert "  #1\tfrom_file\t" in nested.output
    assert "  #2\tfrom_file\t" in nested.output


def test_update_accepts_only_config(mock_pipeline_yaml: Path) -> None:
    """``update`` rejects captcha flags (it never constructs a client)."""
    runner.invoke(app, ["init", "--config", str(mock_pipeline_yaml)])
    ok = runner.invoke(app, ["update", "--config", str(mock_pipeline_yaml)])
    assert ok.exit_code == 0
    bad = runner.invoke(app, ["update", "--config", str(mock_pipeline_yaml), "--no-captcha-ai"])
    assert bad.exit_code != 0
    assert "No such option: --no-captcha-ai" in bad.output


def test_status_shows_pipeline_after_init(mock_pipeline_yaml: Path) -> None:
    runner.invoke(app, ["init", "--config", str(mock_pipeline_yaml)])
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "'mock-demo'" in result.output


def test_status_prints_action_buckets_with_counts(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """The status line names nonzero action buckets with counts (design D).

    Buckets follow the pipeline: fetched rows are ``need-scoring``, scored
    letterless rows ``need-generate``, lettered rows ``ready-to-apply``, and
    decided rows land in the apply ledger. When nothing is left to bucket the
    action suffix is omitted entirely.
    """
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-demo"))

    assert runner.invoke(app, ["fetch", "--pipeline-id", str(pipeline.id)]).exit_code == 0
    out = runner.invoke(app, ["status"]).output
    assert "'mock-demo'" in out
    assert "3 need-scoring" in out

    assert runner.invoke(app, ["score", "--pipeline-id", str(pipeline.id)]).exit_code == 0
    out = runner.invoke(app, ["status"]).output
    assert "3 need-generate" in out
    assert "need-scoring" not in out

    assert runner.invoke(app, ["generate", "--pipeline-id", str(pipeline.id)]).exit_code == 0
    out = runner.invoke(app, ["status"]).output
    assert "3 ready-to-apply" in out

    assert runner.invoke(app, ["apply", "--pipeline-id", str(pipeline.id)]).exit_code == 0
    out = runner.invoke(app, ["status"]).output
    assert "3 applied" in out
    assert "need" not in out  # all three decided: no action buckets left
    assert "ready-to-apply" not in out


def test_status_empty_db_without_init(runtime_dir: Path) -> None:
    """``status`` succeeds against a freshly-migrated file DB with no pipelines.

    Exercises the exact open-storage path the migration bug used to break:
    ``status`` → the ``_with_services`` decorator → ``AppServices.open()`` →
    ``apply_migrations`` on a real file DB. Without init there are no rows,
    so it reports ``No pipelines stored.`` and exits 0.
    """
    del runtime_dir
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "No pipelines stored." in result.output


def test_pipelines_list_empty_without_init(runtime_dir: Path) -> None:
    del runtime_dir
    result = runner.invoke(app, ["pipelines-list"])
    assert result.exit_code == 0
    assert "No pipelines stored" in result.output


def _build_stale_intermediate_db(db: Path) -> None:
    """Create the prototype-era intermediate DB at ``db`` (the user's broken state).

    Stamps the DB at revision ``a1b2`` but leaves ``pipelines`` WITHOUT
    ``openai_captcha`` (the prototype-era ``a1b2`` added only ``service_section``).
    Prior to the guarded-migration fix, opening this DB — e.g. via ``status`` —
    crashed with ``KeyError: 'openai_captcha'``.
    """
    import sqlalchemy as sa
    from alembic import command
    from alembic.config import Config

    ini = Path(__file__).resolve().parents[2] / "src" / "jobfucker" / "storage" / "migrations" / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    command.upgrade(cfg, "a1b2c3d4e5f6")

    engine = sa.create_engine(f"sqlite:///{db}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("ALTER TABLE pipelines DROP COLUMN openai_captcha"))
    finally:
        engine.dispose()


def test_status_recovers_from_stale_intermediate_db(runtime_dir: Path) -> None:
    """Regression: ``status`` recovers a real stale a1b2 DB to head instead of crashing.

    This reproduces the user's exact scenario end-to-end through the real CLI:
    a file DB at ``DATA_DIR/jobfucker.db`` is seeded in the broken prototype-era
    state (stamped ``a1b2``, ``pipelines`` without ``openai_captcha``), then the
    high-level ``status`` command is run. Before the migration fix it crashed
    (``KeyError: 'openai_captcha'``) and every open-storage command failed; now
    the guarded chain upserts it to head and ``status`` exits 0.
    """
    db = runtime_dir / "data" / "jobfucker.db"
    _build_stale_intermediate_db(db)

    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output
    assert "No pipelines stored." in result.output

    # The migrated DB now carries the target schema: identity-only ``pipelines``
    # (config columns moved to ``pipeline_snapshot``). A plain sync engine
    # inspects the file DB (schema introspection is a sync operation; the async
    # engine's ``sync_engine`` is aiosqlite-backed and needs a greenlet).
    import sqlalchemy as sa

    engine = sa.create_engine(f"sqlite:///{db}")
    try:
        pipeline_cols = {col["name"] for col in sa.inspect(engine).get_columns("pipelines")}
        snapshot_cols = {col["name"] for col in sa.inspect(engine).get_columns("pipeline_snapshot")}
    finally:
        engine.dispose()
    assert "current_snapshot_id" in pipeline_cols
    assert "query" not in pipeline_cols  # config content lives in the snapshot now
    assert "openai_captcha" in snapshot_cols
    assert "config_snapshot" not in pipeline_cols


# --- stage / run commands (stub AI + engine seam, no network) ---------------
def test_run_full_pipeline(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-demo"))
    result = runner.invoke(app, ["run", "--pipeline-id", str(pipeline.id)])
    assert result.exit_code == 0
    assert "fetch: search 1/1 ('python'): fetched=3/1000 pages=1 already_stored=0" in result.output
    assert "score: total=3" in result.output
    assert "generate-cv:" in result.output
    assert "apply:" in result.output


def test_fetch_by_pipeline_id_allows_window_slice_and_rejects_position_batching(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-demo"))
    ok = runner.invoke(
        app,
        ["fetch", "--pipeline-id", str(pipeline.id), "--take", "2", "--use-sixel"],
    )
    assert ok.exit_code == 0
    assert "fetch: search 1/1 ('python'): fetched=2/2 pages=1 already_stored=0" in ok.output
    # Fetch carries fetch-window/slice flags, never the position-stage batch
    # controls (its --from/--to/--take ARE slice flags now — position batching
    # stays on score/generate/apply).
    skipped = runner.invoke(app, ["fetch", "--pipeline-id", str(pipeline.id), "--skip-already-processed"])
    assert skipped.exit_code != 0
    assert "No such option: --skip-already-processed" in skipped.output


def test_fetch_rejects_invalid_window_and_slice_values(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-demo"))
    cases = (
        (["--first-page", "-1"], "--first-page must be >= 0"),
        (["--page-size", "25"], "--page-size must be one of 20, 50, 100"),
        (["--take-pages", "0"], "--take-pages must be >= 1"),
        (["--from", "-1"], "--from must be >= 0"),
        (["--to", "-1"], "--to must be >= 0"),
        (["--take", "0"], "--take must be >= 1"),
        (["--to", "5", "--take", "5"], "Cannot combine --to with --take"),
        (["--first-page", "1", "--from", "5"], "Cannot combine --first-page with --from/--to"),
        (["--to", "2", "--from", "5"], "--to must be >= --from"),
        (["--take", "21", "--page-size", "20", "--take-pages", "1"], "exceeds the fetch window"),
    )
    for flags, message in cases:
        bad = runner.invoke(app, ["fetch", "--pipeline-id", str(pipeline.id), *flags])
        assert bad.exit_code != 0
        assert message in bad.output


def test_score_and_generate_by_pipeline_id_take_batch_no_captcha(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-demo"))
    scored = runner.invoke(app, ["score", "--pipeline-id", str(pipeline.id), "--from", "0", "--take", "2"])
    assert scored.exit_code == 0
    assert "score: total" in scored.output
    generated = runner.invoke(app, ["generate", "--pipeline-id", str(pipeline.id)])
    assert generated.exit_code == 0
    assert "generate-cv:" in generated.output
    # score/generate carry no captcha flags and no --config.
    no_captcha = runner.invoke(app, ["score", "--pipeline-id", str(pipeline.id), "--use-sixel"])
    assert no_captcha.exit_code != 0
    assert "No such option: --use-sixel" in no_captcha.output
    no_config = runner.invoke(app, ["score", "--config", "x.yaml", "--pipeline-id", str(pipeline.id)])
    assert no_config.exit_code != 0
    assert "No such option: --config" in no_config.output


def test_apply_by_pipeline_id_takes_batch_and_captcha(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-demo"))
    run_result = runner.invoke(app, ["run", "--pipeline-id", str(pipeline.id)])
    assert run_result.exit_code == 0
    plain = runner.invoke(app, ["apply", "--pipeline-id", str(pipeline.id)])
    assert plain.exit_code == 0
    # The first `run` already applied to all three mock vacancies: nothing
    # attempted, and the not-eligible COUNT in the head line (rows are -v-only).
    assert "apply: 3 in this run — nothing attempted, 3 not eligible" in plain.output
    assert "applied in an earlier run" not in plain.output  # details never at default verbosity
    verbose = runner.invoke(app, ["apply", "--pipeline-id", str(pipeline.id), "-v"])
    assert verbose.exit_code == 0
    assert verbose.output.count("not eligible ") == 3  # one stream row per vacancy, exactly once
    assert "not eligible (3):" not in verbose.output  # the summary never repeats rows
    batched = runner.invoke(app, ["apply", "--pipeline-id", str(pipeline.id), "--from", "0", "--to", "2"])
    assert batched.exit_code == 0
    with_captcha = runner.invoke(app, ["apply", "--pipeline-id", str(pipeline.id), "--use-sixel"])
    assert with_captcha.exit_code == 0


def test_apply_prints_skip_reason_line(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """A this-run board refusal prints verb-first with its reason, plus the ledger line."""
    behavior = MockBehaviorConfig(
        default_apply=MockApplyBehaviorConfig(outcome="applied"),
        per_vacancy={"mock-1": MockApplyBehaviorConfig(outcome="skipped", message="redirect to external form")},
    )
    _patch_cli(monkeypatch, storage, behavior=behavior)
    pipeline = async_run(create_pipeline(storage, name="mock-skip-line"))
    result = runner.invoke(app, ["run", "--pipeline-id", str(pipeline.id)])
    assert result.exit_code == 0
    assert "[1/3] declined mock-1 — redirect to external form" in result.output
    assert "apply: 3 in this run — 2 applied, 1 declined, 0 failed" in result.output


def test_apply_limit_stop_ledger_sums_and_stop_line(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """A board-limit stop names its origin, and the summary buckets sum to the window."""
    behavior = MockBehaviorConfig(
        default_apply=MockApplyBehaviorConfig(outcome="applied"),
        per_vacancy={"mock-2": MockApplyBehaviorConfig(outcome="limit_exceeded")},
    )
    _patch_cli(monkeypatch, storage, behavior=behavior)
    pipeline = async_run(create_pipeline(storage, name="mock-limit-stop"))
    result = runner.invoke(app, ["run", "--pipeline-id", str(pipeline.id)])
    assert result.exit_code == 0
    assert "[1/3] applied mock-1 — " in result.output
    assert "stopping: board signalled the daily limit" in result.output
    # Partition invariant on printed numbers: 1 + 0 + 0 + 2 = 3 (the window).
    assert "apply: 3 in this run — 1 applied, 0 declined, 0 failed, 2 not attempted (limit)" in result.output


def test_apply_zero_attempt_stop_reports_stop_reason(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """A stop before the first attempt keeps the partition: zeros + pending sum to the window."""
    behavior = MockBehaviorConfig(per_vacancy={"mock-1": MockApplyBehaviorConfig(outcome="limit_exceeded")})
    _patch_cli(monkeypatch, storage, behavior=behavior)
    pipeline = async_run(create_pipeline(storage, name="mock-zero-attempt"))
    result = runner.invoke(app, ["run", "--pipeline-id", str(pipeline.id)])
    assert result.exit_code == 0
    assert "stopping: board signalled the daily limit" in result.output
    assert "apply: 3 in this run — 0 applied, 0 declined, 0 failed, 3 not attempted (limit)" in result.output


def test_apply_rejects_contradictory_filter_flags(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """--force needs ids; ids reject batch flags — both fail fast before any apply."""
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-flags"))
    force_alone = runner.invoke(app, ["apply", "--pipeline-id", str(pipeline.id), "--force"])
    assert force_alone.exit_code != 0
    assert "--force requires --vacancy-id" in force_alone.output
    combined = runner.invoke(app, ["apply", "--pipeline-id", str(pipeline.id), "--vacancy-id", "mock-1", "--take", "1"])
    assert combined.exit_code != 0
    assert "Cannot combine --vacancy-id" in combined.output


def test_apply_unknown_vacancy_id_fails_before_applying(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """A requested id missing from the pipeline aborts the run with the id named."""
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-unknown"))
    fetched = runner.invoke(app, ["fetch", "--pipeline-id", str(pipeline.id)])
    assert fetched.exit_code == 0
    result = runner.invoke(app, ["apply", "--pipeline-id", str(pipeline.id), "--vacancy-id", "nope-9", "--force"])
    assert result.exit_code != 0
    assert "not found in pipeline" in result.output
    assert "nope-9" in result.output


def test_apply_vacancy_id_force_re_attempts_decided(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """After a full run, --vacancy-id + --force re-attempts exactly the named vacancy."""
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-force"))
    first = runner.invoke(app, ["run", "--pipeline-id", str(pipeline.id)])
    assert first.exit_code == 0
    retried = runner.invoke(app, ["apply", "--pipeline-id", str(pipeline.id), "--vacancy-id", "mock-2", "--force"])
    assert retried.exit_code == 0
    assert "apply: forced 1 id(s) — eligibility filters bypassed, decided re-attempted" in retried.output
    assert "apply: 1 in this run — 1 applied, 0 declined, 0 failed" in retried.output


def test_apply_relax_flags_apply_unscored_letterless(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """--min-score/--include-unscored/--allow-without-letter reach the stage: fetched-only vacancies apply."""
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-relax"))
    fetched = runner.invoke(app, ["fetch", "--pipeline-id", str(pipeline.id)])
    assert fetched.exit_code == 0
    strict = runner.invoke(app, ["apply", "--pipeline-id", str(pipeline.id)])
    assert strict.exit_code == 0
    assert "apply: 3 in this run — nothing attempted, 3 not eligible" in strict.output
    assert "apply: run relaxed" not in strict.output
    relaxed = runner.invoke(
        app,
        [
            "apply",
            "--pipeline-id",
            str(pipeline.id),
            "--min-score",
            "1",
            "--include-unscored",
            "--allow-without-letter",
        ],
    )
    assert relaxed.exit_code == 0
    assert "apply: run relaxed — unscored allowed, letterless allowed" in relaxed.output
    assert "apply: 3 in this run — 3 applied, 0 declined, 0 failed" in relaxed.output


def test_run_mutually_exclusive_pipeline_id_and_new_from_config(
    storage: Storage, monkeypatch: pytest.MonkeyPatch, mock_pipeline_yaml: Path
) -> None:
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-demo"))
    neither = runner.invoke(app, ["run"])
    assert neither.exit_code != 0
    assert "--pipeline-id" in neither.output and "--new-from-config" in neither.output
    both = runner.invoke(app, ["run", "--pipeline-id", str(pipeline.id), "--new-from-config", str(mock_pipeline_yaml)])
    assert both.exit_code != 0
    assert "cannot use both" in both.output.lower()


def test_run_new_from_config_creates_and_runs(
    storage: Storage, monkeypatch: pytest.MonkeyPatch, mock_pipeline_yaml: Path
) -> None:
    _patch_cli(monkeypatch, storage)
    result = runner.invoke(app, ["run", "--new-from-config", str(mock_pipeline_yaml)])
    assert result.exit_code == 0
    assert "fetch: search 1/1 ('python'): fetched=3/1000 pages=1 already_stored=0" in result.output
    # The config was persisted as a new stored pipeline.
    assert len(async_run(storage.pipelines.list())) == 1


def test_run_new_from_config_reuses_identity_on_repeat(
    storage: Storage, monkeypatch: pytest.MonkeyPatch, mock_pipeline_yaml: Path
) -> None:
    """A second ``run --new-from-config`` with the same config reuses the identity.

    ``run --new-from-config`` is get-or-keep (append semantics, never
    soft-deletes): running twice with the same config keeps one active identity,
    the id stays stable, and no snapshot is appended (no-op).
    """
    _patch_cli(monkeypatch, storage)
    first = runner.invoke(app, ["run", "--new-from-config", str(mock_pipeline_yaml)])
    assert first.exit_code == 0
    first_rows = async_run(storage.pipelines.list())
    assert len(first_rows) == 1
    first_id = first_rows[0].id

    second = runner.invoke(app, ["run", "--new-from-config", str(mock_pipeline_yaml)])
    assert second.exit_code == 0
    # Insert-only mirror sync: the re-run fetches nothing new (rows already
    # stored) and the position stages re-run against the same rows.
    assert "fetch: search 1/1 ('python'): fetched=0/1000 pages=1 already_stored=3" in second.output

    # One active same-name identity remains; the id is stable and nothing was
    # soft-deleted; the identical config appended no snapshot.
    rows = async_run(storage.pipelines.list())
    assert len(rows) == 1
    assert rows[0].name == "mock-demo"
    assert rows[0].id == first_id
    assert rows[0].soft_deleted_at is None
    assert len(async_run(storage.snapshots.list(first_id))) == 1


def test_per_stage_help_shows_only_its_options() -> None:
    fetch_help = runner.invoke(app, ["fetch", "--help"])
    assert fetch_help.exit_code == 0
    for token in (
        "--pipeline-id",
        "--first-page",
        "--page-size",
        "--take-pages",
        "--from",
        "--to",
        "--take",
        "--use-sixel",
        "--use-kitty",
        "--no-captcha-ai",
    ):
        assert token in fetch_help.output
    for token in ("--page ", "--per-page", "--skip-already-processed", "--config"):
        assert token not in fetch_help.output

    score_help = runner.invoke(app, ["score", "--help"])
    assert score_help.exit_code == 0
    for token in ("--pipeline-id", "--from", "--to", "--take", "--skip-already-processed"):
        assert token in score_help.output
    for token in ("--use-sixel", "--config", "--first-page"):
        assert token not in score_help.output

    run_help = runner.invoke(app, ["run", "--help"])
    assert run_help.exit_code == 0
    for token in (
        "--pipeline-id",
        "--new-from-config",
        "--from",
        "--to",
        "--take",
        "--first-page",
        "--page-size",
        "--take-pages",
        "--use-sixel",
    ):
        assert token in run_help.output


# --- top-level Err mapping (real CLI boundary) ------------------------------
def test_captcha_fail_fast_on_client_commands(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """Engine-rebuild ``Err`` (captcha fail-fast) maps to a non-zero exit.

    Stage/run commands rebuild their engine from the stored pipeline via
    :func:`build_engine_from_pipeline`; that seam returns ``Err`` when no captcha
    handler can be selected. Here the seam is stubbed to return the real
    captcha-shaped ``Err`` (the same message :func:`select_captcha_handler`
    produces) and we assert the CLI surfaces it as a message + non-zero exit for
    every client-constructing command — before any client work. The real captcha
    selection inside ``build_engine_from_pipeline`` is covered by
    ``test_bootstrap_captcha_fail_fast``.
    """
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-demo"))
    captcha_error = "Не удалось определить протокол вывода капчи. Используйте --use-sixel или --no-captcha-ai"
    monkeypatch.setattr(
        "jobfucker.cli.build_engine_from_pipeline",
        _stub_build_engine_error(captcha_error),
    )
    for cmd in (
        ["fetch", "--pipeline-id", str(pipeline.id)],
        ["apply", "--pipeline-id", str(pipeline.id)],
        ["run", "--pipeline-id", str(pipeline.id)],
    ):
        result = runner.invoke(app, cmd)
        assert result.exit_code != 0
        assert "протокол" in result.output or "протокол" in result.stderr


# --- type-era helper: satisfy NoReturn import not needed; keep trivial ------
def test_score_and_generate_succeed_with_working_engine(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """score/generate build their own engine and succeed (no captcha path).

    Mirrors the plan's intent that captcha fail-fast does not apply to
    ``score``/``generate``: with a working (stubbed) engine-rebuild they reach
    the engine and exit 0, proving the CLI surface removes the captcha flags.
    """
    _patch_cli(monkeypatch, storage)
    pipeline = async_run(create_pipeline(storage, name="mock-demo"))
    assert runner.invoke(app, ["score", "--pipeline-id", str(pipeline.id), "--from", "0", "--take", "2"]).exit_code == 0
    assert runner.invoke(app, ["generate", "--pipeline-id", str(pipeline.id)]).exit_code == 0


def test_score_runs_without_captcha_when_none_detectable(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: ``score --pipeline-id`` runs (exit 0) with no captcha resolvable.

    score/generate are AI-only: they never construct a client, so a missing
    captcha handler (non-terminal env + no ``openai_captcha``) must NOT fail
    fast. This exercises the **real** :func:`build_engine_from_pipeline` (not a
    stub) with the real selection path: ``detect_terminal_protocol`` returns
    ``None`` and the engine-rebuild must fall back instead of ``Err``. Zero
    vacancies are scored so no AI call/network happens.
    """
    monkeypatch.setattr("jobfucker.cli.AppServices.open", _stub_bootstrap(storage))
    from jobfucker.captcha import selector
    from test.pipeline_helpers import store_pipeline

    stored, _snapshot = async_run(store_pipeline(storage, build_pipeline_config()))

    def _none_detected(*, env: dict[str, str] | None = None, capabilities_path: Path | None = None) -> None:
        del env, capabilities_path

    monkeypatch.setattr(selector, "detect_terminal_protocol", _none_detected)

    result = runner.invoke(app, ["score", "--pipeline-id", str(stored.id)])
    assert result.exit_code == 0, result.output
    assert "score: total=0" in result.output


# --- engine disposal (async-migration follow-up) ----------------------------
def _spy_app_services_close(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Spy on :meth:`AppServices.close`: record each call, still close for real.

    ``monkeypatch`` restores the original method after the test; the delegate
    runs the real ``close``, so a command over a real owned file DB still
    disposes its engine (the dispose observable is covered separately by the
    ``AsyncEngine.dispose`` spy).
    """
    calls: list[int] = []
    original_close: Callable[[AppServices], Awaitable[None]] = AppServices.close

    async def _spy(self: AppServices) -> None:
        calls.append(1)
        await original_close(self)

    monkeypatch.setattr(AppServices, "close", _spy)
    return calls


def _spy_async_engine_dispose(monkeypatch: pytest.MonkeyPatch, disposed: list[AsyncEngine]) -> None:
    """Spy on :meth:`AsyncEngine.dispose`: record each engine, still dispose.

    Lets a test observe that :meth:`AppServices.close` disposed (or did not
    dispose) a specific engine without reaching into SQLAlchemy internals.
    """
    original_dispose: Callable[[AsyncEngine], Awaitable[None]] = AsyncEngine.dispose

    async def _spy(self: AsyncEngine) -> None:
        disposed.append(self)
        await original_dispose(self)

    monkeypatch.setattr(AsyncEngine, "dispose", _spy)


def test_command_disposes_engine_on_success(runtime_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A command run through ``_with_services`` closes the service graph.

    ``status`` runs the real open path (file DB + migrations) via the decorator
    on the isolated ``DATA_DIR``; the spy records that ``close`` ran exactly
    once, and the delegate disposes the real owned engine before the one-shot
    loop closes (the ``storage.db`` invariant — no leaked aiosqlite connection).
    """
    del runtime_dir
    calls = _spy_app_services_close(monkeypatch)
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0
    assert "No pipelines stored." in result.output
    assert calls == [1]


def test_command_disposes_engine_on_failure(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_fail`` (``typer.Exit``) inside a command still triggers close.

    An empty store makes ``resolve`` return ``Err`` → ``_fail``; the
    decorator's ``finally`` must still close the graph exactly once before the
    ``Exit`` propagates to the CLI runner.
    """
    _patch_cli(monkeypatch, storage)
    calls = _spy_app_services_close(monkeypatch)
    result = runner.invoke(app, ["score", "--pipeline-id", "999"])
    assert result.exit_code != 0
    assert "No stored pipelines" in result.output
    assert calls == [1]


async def test_app_services_close_does_not_dispose_injected_override(
    storage: Storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``close()`` never disposes an injected ``storage_override``'s engine.

    Tests share one in-memory StaticPool engine (the ``storage`` fixture);
    disposing it mid-test would destroy the shared DB. The graph built directly
    on the override owns nothing, so ``close()`` (even twice) is a no-op and the
    fixture stays usable afterwards.
    """
    disposed: list[AsyncEngine] = []
    _spy_async_engine_dispose(monkeypatch, disposed)
    factory = _make_factory()
    svc = AppServices(
        storage=storage,
        factory=factory,
        pipelines=PipelineService(storage, factory),
        vacancies=_vacancy_documents(storage),
    )
    await svc.close()
    await svc.close()  # idempotent: a second close is also a no-op
    assert disposed == []
    # The shared engine is still usable after close().
    assert await storage.pipelines.list() == []


async def test_app_services_close_disposes_owned_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``close()`` disposes the engine when the graph opened the file DB itself."""
    engine = create_async_sqlite_engine(tmp_path / "owned.db")
    storage = storage_from_engine(engine)
    disposed: list[AsyncEngine] = []
    _spy_async_engine_dispose(monkeypatch, disposed)
    factory = _make_factory()
    svc = AppServices(
        storage=storage,
        factory=factory,
        pipelines=PipelineService(storage, factory),
        vacancies=_vacancy_documents(storage),
        _owns_storage=True,
    )
    await svc.close()
    assert disposed == [engine]
    await svc.close()  # idempotent: the engine is disposed exactly once
    assert disposed == [engine]


# --- global logging options (--log-stderr / --log-level) ---------------------
def _stderr_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if h.get_name() == "shared.logging.stderr"]


def test_log_stderr_wires_console_handler_and_keeps_stdout_clean(runtime_dir: Path) -> None:
    """``--log-stderr`` installs a colored stderr console handler (never stdout).

    stdout is the CLI user interface; log lines must not be mixed into it.
    """
    del runtime_dir
    result = runner.invoke(app, ["--log-stderr", "status"])
    assert result.exit_code == 0
    # The status line is the only stdout content — no log lines leaked into it.
    assert result.stdout == "No pipelines stored.\n"
    assert len(_stderr_handlers()) == 1


def test_log_stderr_debug_level_reaches_console(runtime_dir: Path) -> None:
    """``--log-level debug`` lowers the console handler to DEBUG (records flow)."""
    del runtime_dir
    result = runner.invoke(app, ["--log-stderr", "--log-level", "debug", "status"])
    assert result.exit_code == 0
    assert result.stdout == "No pipelines stored.\n"
    # The override is proven by the console handler's own level: noisy
    # third-party DEBUG records (asyncio et al.) are silenced even at debug.
    assert len(_stderr_handlers()) == 1
    assert _stderr_handlers()[0].level == logging.DEBUG


def test_log_level_without_stderr_overrides_file_log_level(runtime_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--log-level`` applies to the FILE log too (default info; debug overrides).

    Without ``--log-stderr`` the console stays off, but ``--log-level debug``
    still lowers the file log — that is how a manual run captures the AI
    request/response exchange without a live console.
    """
    default = runner.invoke(app, ["status"])
    assert default.exit_code == 0
    log_path = runtime_dir / "data" / "logs" / "jobfucker.log"
    assert "[DEBUG]" not in log_path.read_text(encoding="utf-8")

    # Noisy third-party loggers are silenced by default, so the proof DEBUG
    # record is the asyncio loop-start one — opt it back in via the env var.
    # Logger levels are process-global; reset the pin an earlier invocation
    # in this worker may have applied (real CLI runs start fresh).
    logging.getLogger("asyncio").setLevel(logging.NOTSET)
    monkeypatch.setenv("NOT_SILENCE_DEPENDENCIES_LOGS", "1")
    debug = runner.invoke(app, ["--log-level", "debug", "status"])
    assert debug.exit_code == 0
    assert "[DEBUG]" in log_path.read_text(encoding="utf-8")


def test_unknown_log_level_fails_fast(runtime_dir: Path) -> None:
    """An unknown ``--log-level`` value fails fast with a non-zero exit."""
    del runtime_dir
    result = runner.invoke(app, ["--log-stderr", "--log-level", "verbose", "status"])
    assert result.exit_code != 0
    assert "Unknown --log-level" in result.output or "Unknown --log-level" in result.stderr


def test_log_flags_must_precede_the_command(runtime_dir: Path) -> None:
    """Global options are placed before the command (git-style), not after."""
    del runtime_dir
    result = runner.invoke(app, ["status", "--log-stderr"])
    assert result.exit_code != 0
    assert "No such option: --log-stderr" in result.output
