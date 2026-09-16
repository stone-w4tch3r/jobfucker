"""Shared pytest fixtures for the jobfucker test suite.

Formalized in Phase 1 (plan task 1.2) on top of the Phase 0 spike fixtures that
this file shipped in 0.4. This is the canonical shared fixture set that every
later phase reuses:

- ``in_memory_db`` — a fresh in-memory **async** SQLAlchemy engine per test
  (``sqlite+aiosqlite:///:memory:`` with ``StaticPool`` — the async-equivalent
  of the old sync engine, strong cross-test isolation by construction).
- ``db_session`` — a per-test transaction rolled back on teardown so no state
  leaks between tests (the "transaction-per-test" pattern). Primary DB-isolation
  pattern; ``in_memory_db`` is the fresh-schema fallback.
- ``captured_logs`` — capture ``logging`` records so engine/manager tests can
  assert on behavior without spamming output (the ``caplog``-style fixture).
- ``runtime_dir`` — an isolated ``config/``+``data/`` runtime directory per test,
  with ``CONFIG_DIR``/``DATA_DIR`` env vars pointed at it so app code that reads
  those env overrides is isolated from the real ``~/.config`` / ``~/.local``.
  Formalizes Phase 0's ``temp_runtime_dir`` and wires the env overrides.
- ``make_config_file`` — a factory that writes a temp ``pipeline.yaml``/resume/
  prompt-style file into the isolated ``runtime_dir`` config dir.
- ``storage`` — an async ``Storage`` facade (engine + session factory + 4 repos),
  schema created via the ORM-metadata fast path over ``in_memory_db``.
- ``client_deps`` — ``ClientDeps`` from isolated runtime dir + stub seams.

Async notes (async-migration spec):
- ``in_memory_db``/``storage`` expose the **async** engine/facade; tests that
  drive the repositories become ``async def`` and ``await`` their calls
  (pytest-asyncio ``asyncio_mode="auto"``). BDD steps stay synchronous and call
  the async domain through ``jobfucker.testing.step_runner``.
- The engine is disposed in teardown (never leak an open async engine).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Generator
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import jinja2
import pytest
import pytest_bdd
import sqlalchemy
from pytest_bdd import given as _pytest_bdd_given
from pytest_bdd import then as _pytest_bdd_then
from pytest_bdd import when as _pytest_bdd_when
from pytest_bdd.parsers import StepParser
from rusty_results.prelude import Ok, Result
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import StaticPool

from jobfucker.clients.base import ClientCredentials, ClientDeps

if TYPE_CHECKING:
    from jobfucker.storage.db import Storage


# --- Async-step tripwire (async-migration follow-up) -------------------------
# pytest-bdd 8.1.0 executes steps via ``_pytest.fixtures.call_fixture_func``
# (pytest_bdd/scenario.py:243), which never awaits a coroutine: an ``async def``
# step body is silently dropped while the scenario still reports PASS (false
# green). BDD steps must stay synchronous and call the async domain through
# ``jobfucker.testing.step_runner`` (test/AGENTS.md §6). To make the forbidden
# construct fail loudly instead of silently, the step decorators are patched
# here - conftest loads before any step module runs ``from pytest_bdd import
# given`` - with thin wrappers that reject coroutine functions at registration.
# The install is idempotent: a second import of this module (e.g. from the
# tripwire regression test) must not re-wrap an already-guarded decorator (that
# would double the stacklevel bump below and mis-register step fixtures).
#
# Each wrapper bumps pytest-bdd's ``stacklevel`` by one because the real
# decorator resolves the *calling module's* locals with a frame lookup
# (pytest_bdd/utils.py:36-42) that must keep landing in the step module, not in
# this conftest's frame.

# pytest-bdd's ``converters`` are heterogeneous ({param: callable}); mirror its
# own signature (Any upstream) without leaking Any/object into conftest.
type _StepConverters = dict[str, Callable[[str], object]]


def guard_step_decorator[**P, T](
    step_kind: str,
    decorator: Callable[[Callable[P, T]], Callable[P, T]],
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Wrap a pytest-bdd step decorator so registering an ``async def`` step raises."""

    def guarded(func: Callable[P, T]) -> Callable[P, T]:
        if inspect.iscoroutinefunction(func):
            raise TypeError(
                f"{step_kind} step {func.__name__!r} is 'async def'; pytest-bdd calls steps via "
                "call_fixture_func (pytest_bdd/scenario.py:243), which never awaits a coroutine - "
                "the body is silently dropped and the scenario false-greens. Keep BDD steps "
                "synchronous and call the async domain through "
                "jobfucker.testing.step_runner async_run / async_run_result."
            )
        return decorator(func)

    return guarded


def _guarded_given[**P, T](
    name: str | StepParser,
    converters: _StepConverters | None = None,
    target_fixture: str | None = None,
    stacklevel: int = 1,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Given-step tripwire (see ``guard_step_decorator`` and the section comment)."""
    decorator = _pytest_bdd_given(name, converters, target_fixture, stacklevel + 1)
    return guard_step_decorator("given", decorator)


def _guarded_when[**P, T](
    name: str | StepParser,
    converters: _StepConverters | None = None,
    target_fixture: str | None = None,
    stacklevel: int = 1,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """When-step tripwire (see ``guard_step_decorator`` and the section comment)."""
    decorator = _pytest_bdd_when(name, converters, target_fixture, stacklevel + 1)
    return guard_step_decorator("when", decorator)


def _guarded_then[**P, T](
    name: str | StepParser,
    converters: _StepConverters | None = None,
    target_fixture: str | None = None,
    stacklevel: int = 1,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    """Then-step tripwire (see ``guard_step_decorator`` and the section comment)."""
    decorator = _pytest_bdd_then(name, converters, target_fixture, stacklevel + 1)
    return guard_step_decorator("then", decorator)


_GUARD_SENTINEL = "_jobfucker_async_step_guard"


def _install_async_step_guard() -> None:
    """Patch pytest_bdd.given/when/then with the tripwire (idempotent)."""
    if getattr(pytest_bdd.given, _GUARD_SENTINEL, False):
        return
    pytest_bdd.given = _guarded_given
    pytest_bdd.when = _guarded_when
    pytest_bdd.then = _guarded_then
    for decorator in (pytest_bdd.given, pytest_bdd.when, pytest_bdd.then):
        setattr(decorator, _GUARD_SENTINEL, True)


_install_async_step_guard()


class StubAuthInteraction:
    """A canned :class:`AuthInteractionProvider` for client tests.

    Returns fixed values so client behavior can be asserted without a human.
    """

    async def request_code(self, *, prompt: str) -> Result[str, str]:
        """Return a fixed one-time code."""
        del prompt
        return Ok("123456")

    async def confirm(self, *, prompt: str) -> Result[bool, str]:
        """Return a fixed confirmation."""
        del prompt
        confirmed: bool = True
        return Ok(confirmed)


async def _stub_captcha_handler(image: bytes) -> Result[str, str]:
    """Return a fixed captcha answer for any image bytes."""
    del image
    return Ok("stub-captcha")


# The ``make_config_file`` factory: write ``content`` to a file named ``name``
# under the runtime config dir and return the resolved path. A Protocol (rather
# than a bare Callable) expresses the keyword-only ``name`` default.
class MakeConfigFileFn(Protocol):
    """Signature of the ``make_config_file`` fixture's returned callable."""

    def __call__(self, content: str, name: str = "pipeline.yaml") -> Path: ...


# --- DB isolation ------------------------------------------------------------
@pytest.fixture
def in_memory_db() -> Generator[AsyncEngine]:
    """A fresh in-memory async SQLAlchemy engine, isolated per test.

    ``sqlite+aiosqlite:///:memory:`` with a ``StaticPool`` reuses one connection
    for the lifetime of the fixture (required for an in-memory DB), while the
    function-scoped fixture gives every test its own DB. Callers build the schema
    via ``await create_schema(engine)`` before use (the ``storage`` fixture does
    this automatically). The engine is disposed in teardown (never leak an open
    async engine; the absence of dispose produces shutdown ``greenlet is being
    finalized`` noise per the async-migration spec §2.2).
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
    )
    try:
        yield engine
    finally:
        asyncio.run(engine.dispose())


# --- Logging -----------------------------------------------------------------
class LogCapture:
    """Collect ``logging.LogRecord``s emitted during a test.

    Attributes:
        records: every captured record, in emission order.
    """

    def __init__(self) -> None:
        self.records: list[logging.LogRecord] = []

    def messages(self) -> list[str]:
        """Return the formatted message text of every captured record."""
        return [record.getMessage() for record in self.records]


@pytest.fixture
def captured_logs() -> Generator[LogCapture]:
    """Capture ``logging`` records emitted during a test.

    Returns a :class:`LogCapture` whose ``records``/``messages()`` let tests
    assert on log output. Attaches a ``logging.Handler`` to the root logger and
    removes it on teardown. This is the project's ``caplog``-style fixture.
    """
    capture = LogCapture()

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            capture.records.append(record)

    handler = _Handler(level=logging.DEBUG)
    root = logging.getLogger()
    previous_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        yield capture
    finally:
        root.removeHandler(handler)
        handler.close()
        root.setLevel(previous_level)


# --- Runtime isolation --------------------------------------------------------
@pytest.fixture
def runtime_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated runtime directory for a single test.

    Creates ``<tmp>/runtime/{config,data}`` and points ``CONFIG_DIR``/``DATA_DIR``
    at them, so app code that honours those env overrides is fully isolated from
    the developer's real ``~/.config`` / ``~/.local`` dirs (architecture §6).
    Returns the runtime root; ``runtime_dir / "config"`` and
    ``runtime_dir / "data"`` are the two wired XDG-style subdirs.
    """
    runtime = tmp_path / "runtime"
    config = runtime / "config"
    data = runtime / "data"
    config.mkdir(parents=True)
    data.mkdir(parents=True)
    monkeypatch.setenv("CONFIG_DIR", str(config))
    monkeypatch.setenv("DATA_DIR", str(data))
    return runtime


@pytest.fixture
def make_config_file(runtime_dir: Path) -> MakeConfigFileFn:
    """Factory writing a temp config/content file into the isolated config dir.

    Returns a ``(content, name) -> Path`` callable that writes ``content`` to
    ``runtime_dir/config/<name>`` and returns the path. Used to seed fixtures
    such as ``pipeline.yaml``, a resume, or a prompt template without touching a
    real config location.
    """

    def _write(content: str, name: str = "pipeline.yaml") -> Path:
        path = runtime_dir / "config" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def mock_pipeline_yaml(runtime_dir: Path) -> Path:
    """A hermetic, fully self-contained mock pipeline.yaml plus its referenced files.

    The config source lives as a Jinja2 template under ``test/fixtures/`` (so the
    YAML body is not duplicated in the fixture); this fixture loads it, renders it
    with the per-test absolute file paths (the only props that must be overridden),
    writes it — together with every referenced file — into the isolated runtime
    dir, and returns the yaml path. Referenced files use absolute paths, which
    pass through resolution untouched, regardless of the repo layout or any
    ambient ``secrets/`` dir.
    """
    config_dir = runtime_dir / "config"
    secrets = config_dir / "secrets"
    secrets.mkdir(parents=True, exist_ok=True)

    login = secrets / "login.txt"
    login.write_text("mock-login@example.com", encoding="utf-8")
    password = secrets / "password.txt"
    password.write_text("mock-password", encoding="utf-8")
    api_key = secrets / "api_key.txt"
    api_key.write_text("sk-mock-test", encoding="utf-8")
    resume = config_dir / "resume.md"
    resume.write_text("# Mock Resume\n\nPython developer.\n", encoding="utf-8")
    scoring_prompt = config_dir / "prompts" / "score.md.j2"
    scoring_prompt.parent.mkdir(parents=True, exist_ok=True)
    scoring_prompt.write_text("Score the vacancy against the resume.", encoding="utf-8")

    fixtures_dir = Path(__file__).resolve().parent / "fixtures"
    # select_autoescape only escapes .html/.htm/.xml templates; this trusted
    # local .j2 YAML config is rendered unescaped (no HTML, no user markup).
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(fixtures_dir),
        autoescape=jinja2.select_autoescape(),
        keep_trailing_newline=True,
    )
    rendered = env.get_template("pipeline.mock.yaml.j2").render(
        # as_posix: Windows backslashes are invalid escapes inside YAML
        # double-quoted scalars; forward slashes resolve fine everywhere.
        login_file=login.as_posix(),
        password_file=password.as_posix(),
        api_key_file=api_key.as_posix(),
        resume_path=resume.as_posix(),
        scoring_prompt_file=scoring_prompt.as_posix(),
        apply_prompt_file=resume.as_posix(),
    )

    yaml_path = config_dir / "pipeline.mock.yaml"
    yaml_path.write_text(rendered, encoding="utf-8")
    return yaml_path


# --- Storage ---------------------------------------------------------------
@pytest.fixture
def storage(in_memory_db: AsyncEngine) -> Storage:
    """A ready-to-use async :class:`Storage` facade over a fresh in-memory engine.

    Creates the five tables via the ORM metadata (the fast path for repository
    tests); the migration path is exercised separately in
    ``test/storage/test_migrations.py`` and wire the same schema. See
    ``test/AGENTS.md`` §10 for the schema-creation decision. The engine is the
    shared ``in_memory_db`` (disposed by that fixture).
    """
    from jobfucker.storage.db import create_schema, storage_from_engine

    asyncio.run(create_schema(in_memory_db))
    return storage_from_engine(in_memory_db)


# --- Client deps ------------------------------------------------------------
@pytest.fixture
def client_deps(runtime_dir: Path) -> ClientDeps:
    """A ``ClientDeps`` built from the isolated runtime dir + stub seams.

    Used by client/factory/mock tests to construct clients with the same
    construction contract a real composition root would use.
    """
    return ClientDeps(
        service="mock",
        profile_id=None,
        data_dir=runtime_dir / "data",
        credentials=ClientCredentials(login="test-login", password="test-password"),
        auth_interaction=StubAuthInteraction(),
        captcha_handler=_stub_captcha_handler,
    )


# Re-export sqlalchemy symbols used by the fixture helpers so tests can build
# their own models without importing sqlalchemy twice. Kept as a module import.
__all__ = [
    "captured_logs",
    "client_deps",
    "in_memory_db",
    "make_config_file",
    "mock_pipeline_yaml",
    "runtime_dir",
    "sqlalchemy",
    "storage",
]
