# jobfucker test infrastructure — conventions

This file is the authoritative reference for the jobfucker test infrastructure.
It describes the current state of `test/` and the root pytest/coverage config,
not how they were built.

---

## 1. Tooling & commands

All project-local Python tools run through `uv`:

```sh
uv run poe lint_full        # basedpyright strict + ruff fix/format + custom linters
uv run poe test             # full suite (xdist parallel + coverage)
uv run basedpyright <path>  # single-file quick check
```

`uv run poe lint_full` and `uv run poe test` are the **standard verification
pair** and must both be clean at every checkpoint.

---

## 2. pytest-bdd version & API

Pinned as `pytest-bdd>=8.0.0` in `[dependency-groups].dev`; installed `8.1.0`.

The suite uses the **fixture-injection step model**, which v8 retains: `@given`,
`@when`, `@then` decorators with `target_fixture` (seed state from a `Given`) and
`converters` (per-param runtime conversion). Scenario collection via
`scenarios("relative.feature")`.

### Key API facts

- **Placeholders require `parsers.parse(...)`.** In pytest-bdd 8.x a _plain
  string_ step name is matched **exactly** (a `string` parser). To make
  `{placeholder}` steps match the feature text you must wrap the pattern in
  `parsers.parse("... {x:d}")`.
- **Gherkin `{placeholder}` params arrive as `str` by default.** Use a format
  specifier (`{x:d}` → `int`) or a `converters` entry to get a typed value, and
  annotate the step-function parameter to match.
- **Step-function type annotations are preserved** by the v8 decorators
  (`Callable[[Callable[P,T]], Callable[P,T]]`), so strict typing sees your real
  signatures — this is why the typed-step convention works cleanly.
- **`scenarios()` collects tests dynamically.** The generated `test_*` functions
  exist only at pytest collection time and are invisible to basedpyright. That
  is expected and harmless: **steps, not scenarios, carry the type burden.**

### Typing hazards (documented)

1. Dynamic `scenarios()` collection — generated tests aren't statically visible.
2. `context`/step-arg typing — always annotate step params explicitly.
3. Placeholder params default to `str` — convert explicitly where needed.

---

## 3. Step-typing convention (basedpyright strict)

All Gherkin modules follow this convention and pass `basedpyright` strict with
`reportAny = "error"` and **zero** per-file config relaxation:

```python
from pytest_bdd import given, parsers, scenarios, then, when

scenarios("counter.feature")  # bare statement; generated tests invisible to pyright

@given(parsers.parse("a counter starting at {start:d}"), target_fixture="counter")
def counter_step(start: int) -> Counter:      # explicit param + return types
    return Counter(start)

@when(parsers.parse("I increment the counter by {amount:d}"))
def increment_step(counter: Counter, amount: int) -> None:
    counter.increment(amount)

@then(parsers.parse("the counter value is {expected:d}"))
def check_value_step(counter: Counter, expected: int) -> None:
    assert counter.value == expected
```

Rules:

1. **Every step function has explicit parameter and return types** — this is the
   primary mitigation and is sufficient. No `reportUnknownParameterType` leaks.
2. Annotate placeholders to match what the parser emits (`str` default; `int`
   with `:d` / converters).
3. Use `target_fixture="<name>"` on a `Given` to seed state that later steps
   receive as a fixture parameter.
4. **Any** suppression must be per-line `# type: ignore[specific-code]  # rationale: <reason>`
   (enforced by the `check_type_ignore` custom linter). There is **no** global
   `reportAny=off` and no `Any`.

`reportAny = "error"` and `reportImportCycles = "error"` remain ON for the BDD
path; no other rules are relaxed.

---

## 3a. BDD vs plain pytest (policy)

The standing rule for **new** test code; it overrides the "current state"
framing of the rest of this file.

- **All new test files, modules, and scenarios must be BDD** (`.feature` +
  typed steps) — including in areas currently covered only by plain pytest.
- **Legacy plain-pytest suites may be extended** when the feature under test is
  edited or modified. Extension inside an existing legacy file is allowed;
  a _new_ test file is a new suite and must be BDD.
- **The only new-pytest exception:** tiny, self-contained test cases that fit
  in 10–30 lines of plain pytest, are not part of a larger interconnected
  suite, and do not exercise a full feature. A case is _not_ tiny if it needs
  shared fixtures beyond `storage`/`runtime_dir` or shares state with sibling
  tests — that belongs in a BDD feature.
- Today the suite is has lot's of plain tests vs few BDD scenarios; migrating the legacy
  weight is backlog. This section is the forward rule, not a rewrite mandate.

---

## 3b. Step state flow — mandated shape

State between steps flows through **fixture injection**, never through a
mutable per-scenario state object.

Positive rules:

1. `Given` seeds state via `target_fixture="<name>"`; the returned value is a
   typed domain object or frozen carrier.
2. `When` performs the action and **returns its result** via
   `target_fixture="<result>"` (unwrapped via `async_run_result`, §6).
3. `Then` consumes the result as a parameter and only asserts.
4. A scenario needing several observations carries them in one **frozen, typed
   dataclass** (house pattern: `ScoreOutcome`, `EngineCtx`, `AppendState`,
   `GenerateOutcome`, `NoopState`).
5. Setup variants are distinct `Given` steps (or `Scenario Outline` rows when
   they differ only by data) — never a `kind`/`scenario` enum switching
   behavior inside a fake.

Forbidden:

- One mutable object holding _all_ scenario state, injected into every step,
  with `Then` steps reading fields/lists it accumulated.
- Steps mutating shared lists/fields of a state bag (appending to a
  `results` list, reaching through to a service) instead of returning values.
- Gherkin scenarios that are a thin wrapper around a state bag — the feature
  text then describes the bag's lifecycle, not the product behavior.
- Scenario-kind enums branching inside fakes.

House carrier vocabulary (all `@dataclass(frozen=True,
slots=True)`): a `Given` seeds a **setup carrier** (e.g. `AuthScenario`,
`ApplyScenario`: transport recorder + `make_client` factory + recorders), a
`When` returns an **outcome carrier** (e.g. `CaptchaOutcome`, `SearchOutcome`:
the action result + a snapshot `tuple` of the request history). Counters that
used to live on fakes are derived in `Then` steps by counting the recorded
requests. The hh client area shares its scaffolding (client factory, auth-state
seeding, scripted-response playback, payload builders) in
`test/clients/hh/helpers.py` (§8).

---

## 3c. Worked example

Illustrative example (no `rescore` files exist in the suite) — it shows the
mandated step-by-step shape in miniature; for live, full-size references see
the `test/clients/hh/test_*_bdd.py` collectors. The BDD form of the legacy
plain test `test_rescore_keeps_apply_outcome_untouched` (migration example):
state flows by `target_fixture`; the `When` returns a frozen carrier; `Then`
only asserts; the async domain is driven through `step_runner`; data comes from
the shared builders.

```gherkin
# test/stages/bdd/rescore.feature
Feature: Re-scoring preserves apply outcomes
    In order to keep apply decisions stable across re-scores
    As the engine
    I want the score stage to touch only scoring fields

    Scenario: A re-score under a newer snapshot never touches the apply stage
        Given a pipeline with min_required_score 3
        And a vacancy applied under the current snapshot
        When the score stage re-runs and scores it 2
        Then the vacancy has score 2
        And the apply outcome and its provenance are preserved
```

```python
# test/stages/test_rescore_bdd.py
"""BDD acceptance for re-scoring semantics (test/AGENTS.md §3b)."""

from __future__ import annotations

from dataclasses import dataclass

from pytest_bdd import given, scenarios, then, when
from rusty_results.prelude import Ok

from jobfucker.ai import ScoreResult
from jobfucker.stages.prompts import PromptInputs
from jobfucker.stages.score import run_score
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, VacancyRecord
from jobfucker.testing.step_runner import async_run, async_run_result
from test.pipeline_helpers import ScriptedAi, create_pipeline, snapshot_for
from test.storage.builders import build_vacancy

scenarios("bdd/rescore.feature")


@dataclass(frozen=True, slots=True)
class RescoreOutcome:
    """Frozen carrier: the persisted rows, for cross-step assertion."""

    vacancies: list[VacancyRecord]


def _inputs() -> PromptInputs:
    return PromptInputs(
        resume="resume contents",
        prompt="Score {{ resume_formatted }} for {{ vacancy_formatted }}",
    )


@given("a pipeline with min_required_score 3", target_fixture="pipeline")
def pipeline_step(storage: Storage) -> Pipeline:
    """Create a pipeline identity + head snapshot (threshold 3)."""
    return async_run(create_pipeline(storage, name="rescore-bdd", min_required_score=3))


@given("a vacancy applied under the current snapshot")
def applied_vacancy_step(storage: Storage, pipeline: Pipeline) -> None:
    """Seed a vacancy the apply stage already decided, with provenance."""
    snap = async_run(snapshot_for(storage, pipeline))
    async_run(
        storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=pipeline.id,
                external_id="v1",
                apply_status="applied",
                applied_snapshot_id=snap.id,
                applied_at="2026-08-05 10:00:00",
            )
        )
    )


@when("the score stage re-runs and scores it 2", target_fixture="outcome")
def rescore_step(storage: Storage, pipeline: Pipeline) -> RescoreOutcome:
    """Re-score under a *newer* snapshot; capture the persisted rows."""
    snap = async_run(snapshot_for(storage, pipeline))
    async_run_result(
        run_score(
            storage,
            pipeline,
            ScriptedAi([Ok(ScoreResult(fit_score=2, comment="weaker now"))]),
            _inputs(),
            snapshot_id=snap.id,
            min_required_score=snap.min_required_score,
        )
    )
    return RescoreOutcome(vacancies=async_run(storage.vacancies.list_by_pipeline(pipeline.id)))


@then("the vacancy has score 2")
def assert_score_step(outcome: RescoreOutcome) -> None:
    """The new score is stored like any other."""
    vacancy = outcome.vacancies[0]
    assert vacancy.score == 2
    assert vacancy.score_reasoning == "weaker now"


@then("the apply outcome and its provenance are preserved")
def assert_apply_untouched_step(outcome: RescoreOutcome) -> None:
    """The apply stage's fields survive verbatim under a re-score."""
    vacancy = outcome.vacancies[0]
    assert vacancy.apply_status == "applied"
    assert vacancy.applied_snapshot_id is not None  # stamped under the old snapshot
    assert vacancy.scored_snapshot_id is not None   # stamped under the new one
```

Forbidden shape — the same scenario expressed as a state bag (sketch; never
write this, see §3b):

```python
@given("a pipeline with min_required_score 3", target_fixture="state")   # not allowed
def bad_pipeline_step(storage: Storage) -> MutableStateBag:
    bag = MutableStateBag()
    bag.pipeline = async_run(create_pipeline(storage))
    return bag

@when("the score stage re-runs and scores it 2")
def bad_rescore_step(state: MutableStateBag) -> None:                    # mutates, does not return
    state.results.append(async_run_result(run_score(...)))

@then("the vacancy has score 2")
def bad_assert_step(state: MutableStateBag) -> None:                     # index reach-through
    assert state.results[0].score == 2
```

---

## 4. Qt tests — removed with the UI (rewrite planned)

The Qt suite (`test/ui/**`, `test/families/test_qt_smoke.py`) was **removed** together with the
UI implementation (`src/jobfucker/ui/`), because the UI didn't work and confused users and
developers. The root `conftest.py`'s headless gate (`QT_QPA_PLATFORM=offscreen` for
`qt`-marked items) was removed with it.

The rewrite is planned per `docs/ui/jobfucker.ui.md`. When it lands, restore the headless
conventions documented below (they are the tested recipe):

- **`QT_QPA_PLATFORM=offscreen` applied only to `qt`-marked items** at collection time by the
  root `conftest.py` (`os.environ.setdefault`), so the env is in place before pytest-qt's
  session-scoped `qapp` builds the `QApplication`; CI and headless boxes work, a locally-
  exported value wins (a real display stays possible locally), and non-Qt tests are unaffected.
- **`qtbot`/`qapp` reach fixtures by declaring them as step-function/`pytest` parameters.**
  pytest-bdd injects pytest fixtures into step functions, so a step
  `def click_step(clicker: Clicker, qtbot: QtBot)` just works.
- **State kept across steps via `target_fixture`.** A `Given` returns e.g. a
  `Clicker(QObject)`; later `When`/`Then` steps request it as `clicker: Clicker`.
- **`@qt` Gherkin tag → `qt` pytest marker.** pytest-bdd converts Gherkin tags
  into pytest markers, so `@qt` on a feature/scenario makes the generated tests
  selectable via `-m qt`. The `qt` marker is registered in `pyproject.toml`.
- **Event pumping:** signals fired synchronously are captured with `qtbot.waitSignal(...)` —
  no manual pump needed. A `qasync` pump is only needed when a step drives real async manager
  work (see §6); the step-level Qt loop is otherwise driven by pytest-qt.

PySide6/pytest-qt typing passes strict cleanly (no suppressions at that boundary). The Qt
toolchain (`pyside6`, `qasync`, `pytest-qt`) is still in the dependency tree: the tested
`src/jobfucker/shared/shortcuts` building block imports PySide6, and the rewrite reuses
the toolchain.

---

## 5. Shared fixtures & DB isolation

`test/conftest.py` is the canonical fixture registry, shared by every area:

| Fixture              | Returns                                                                     | Isolation                                                                                                                                                  |
| -------------------- | --------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `in_memory_db`       | `AsyncEngine` — fresh `sqlite+aiosqlite:///:memory:` per test (StaticPool)  | fresh-DB (function-scoped)                                                                                                                                 |
| `storage`            | `Storage` facade (async engine + session factory + 5 repos), schema created | fresh-DB over `in_memory_db` (function-scoped)                                                                                                             |
| `captured_logs`      | `LogCapture` (`.records` / `.messages()`)                                   | attaches a DEBUG handler; lowers root level for the test                                                                                                   |
| `runtime_dir`        | `Path` to `runtime/{config,data}`; sets `CONFIG_DIR`/`DATA_DIR`             | tmp_path + env override, per test                                                                                                                          |
| `make_config_file`   | `(content, name="pipeline.yaml") -> Path`                                   | writes into the isolated runtime config dir                                                                                                                |
| `mock_pipeline_yaml` | `Path` to a hermetic mock `pipeline.yaml` + its referenced files            | writes config + login/password/api_key/resume into the isolated runtime dir (absolute refs), so CLI tests never depend on the repo's gitignored `secrets/` |
| `client_deps`        | `ClientDeps` from isolated runtime dir + stub seams                         | per-test                                                                                                                                                   |

**Decision:** DB isolation uses a **fresh in-memory async engine per test**
(`in_memory_db` with `StaticPool`) as the primary pattern — every write commits
to that engine's DB and the engine is disposed on teardown, so no state ever
leaks between tests, regardless of xdist worker/order. The engine must always
be disposed before the loop/process closes (the `in_memory_db` fixture teardown
does this; tests that open their own engine must `await engine.dispose()` too).

`test/test_harness_smoke.py` proves each shared fixture, including explicit
cross-test DB isolation (write in one test, fresh in the next).

> **Async migration note (authoritative):** storage tests drive the async
> repositories: tests that touch `storage` become `async def` and `await` every
> repository call (pytest-asyncio `asyncio_mode="auto"`). BDD steps stay
> **synchronous** and call the async domain through
> `jobfucker.testing.step_runner.async_run` / `async_run_result` (see §6).

---

## 6. Async story (pytest-asyncio + qasync)

**The whole domain is async** (async-migration spec): engine, stages, `Client` /
`AiClient`, and all repositories are `async def`. Test conventions:

- **Plain coroutines run under `pytest-asyncio`** (`asyncio_mode = "auto"` in
  `pyproject.toml`) — used for non-Qt async logic. Tests that await storage /
  engine / client / ai methods are `async def`.
- **BDD steps are synchronous** — pytest-bdd 8.1.0 cannot run `async def` step
  functions (it calls steps via `_pytest.fixtures.call_fixture_func`, which never
  awaits a coroutine → the step body is **silently dropped** and the scenario
  still reports PASS, a false green). From a step, call the async domain via the
  canonical runner:
  - `async_run_result(coro)` — for async `Result`-returning calls: `Ok` →
    value, `Err` → `pytest.fail` at the step.
  - `async_run(coro)` — for async calls that return a plain value
    (storage repo accessors, `create_pipeline`, `snapshot_for`, ...).
    Both live in `src/jobfucker/testing/step_runner.py`. `async def` BDD steps
    are **forbidden** (silent no-op → false green) and **enforced by a tripwire**:
    `test/conftest.py` patches `pytest_bdd.given/when/then` (idempotent, installed
    before step modules import them at collection) so registering an `async def`
    step raises a `TypeError` — an async step can no longer silently false-green.
- **Never call `async_run` / `asyncio.run` from an already-running loop.** BDD uses
  per-call `asyncio.run`; a future Qt GUI would use a persistent `qasync` loop. The two never
  mix.
- **Qt-manager async tests (removed with the UI)** used a `qasync.QEventLoop` wrapping the
  running `QApplication` to pump a coroutine through the Qt loop without blocking it — the
  same bridge the GUI used in production. `test/ui/conftest.py` provided `run_qt_coro` (run a coroutine to completion) and `run_qt_action` (invoke a
  QML action, then await every in-flight manager task on the loop); the rewrite will restore this pattern.
- **Storage BDD harness:** the shared `with_db` helper
  (`src/jobfucker/storage/testing.py`) opens a **file-DB** `NullPool`
  `AsyncEngine` per call, creates the schema, runs the callable with a fresh
  `Storage`, and always `await engine.dispose()`s. It lives in `storage/**`
  because SQLAlchemy imports are `TID251`-banned outside `storage/`. Persistence
  across BDD steps uses one stable file path per scenario. Never use in-memory
  `NullPool` for persistence (fresh empty DB per connection → `no such table`);
  never reuse an `AsyncEngine` across `asyncio.run` steps without disposing.
- **Dispose discipline:** always `await engine.dispose()` before a loop/process
  closes (its absence produces shutdown `greenlet is being finalized` noise).
  The `in_memory_db` fixture and `with_db` do this; the GUI's `app.py` disposed
  after the qasync loop closed (rewrite will do the same).
- **qasync is typed via a vendored stub.** `src/stubs/qasync/__init__.pyi`
  (resolved through `stubPath = "src/stubs"` in `pyproject.toml`) declares
  `qasync.QEventLoop` as an `asyncio.BaseEventLoop` subclass, so the loop is
  fully `asyncio`-typed — `run_until_complete`, `run_forever`, `call_soon`,
  `close`, and `with loop:` all resolve with **zero** `# type: ignore`s. The
  stub covers only the qasync surface the project uses; it is dev-time typing
  only and is not shipped (package is `src/jobfucker`).
- **`Result` comes from `rusty_results.prelude`** (bare `rusty_results` trips
  `reportPrivateImportUsage` under strict) and is unwrapped at the step boundary
  (`Ok` → value, `Err` → `pytest.fail`), never treated as an exception.

---

## 7. Markers & the `--run-e2e` gate

Markers (declared in `[tool.pytest.ini_options].markers`):

| Marker        | Runs by default | Meaning                          |
| ------------- | --------------- | -------------------------------- |
| `unit`        | yes             | pure, side-effect-free logic     |
| `integration` | yes             | DB-backed / boundary integration |
| `e2e`         | **skipped**     | live/gate tests; opt-in only     |

(The `qt` marker was removed with the UI; the rewrite will re-register it.)

The **root `conftest.py`** (repo root — applies to `shared_tests` and `test`)
owns the cross-cutting gate:

- `pytest_addoption` adds `--run-e2e` (store_true, default False).
- `pytest_collection_modifyitems` (a) **skip-marks every `e2e` item unless**
  `--run-e2e`, so the gate is selectable from day one but never runs by default;
  (b) sets `QT_QPA_PLATFORM=offscreen` for `qt` items via `os.environ.setdefault`
  (§4).

Commands:

```sh
uv run poe test               # full suite; e2e skipped
uv run poe test --run-e2e     # e2e included; gate passes
uv run poe test -m "not e2e"  # explicit exclusion, same as the default
uv run pytest -m unit         # unit family only
```

> **Poe arg note — `--` is not valid here.** Pass pytest args directly after the
> task name. `uv run poe test -- --run-e2e` forwards `--` literally to pytest
> (it becomes a 0-item positional `testpath` → "no tests ran"). Use
> `uv run poe test --run-e2e` instead.

---

## 8. Coverage, xdist & reuse conventions

### Coverage (wired)

- `addopts` adds `--cov --cov-report=term-missing`.
- `[tool.coverage.run] source = ["src"]`; `omit` covers `test/*`,
  `test/**/*`, `shared_tests/*`, `shared_tests/**/*`, and `tests/*`. Coverage
  measures the **app package (`src/`), including the folded-in
  `src/jobfucker/shared/` building blocks**.
- `[tool.coverage.report] show_missing = true`, `skip_covered = true`,
  `fail_under = 0` (coverage is a **guideline, not a gate** — engineering-
  principles §5). CI can read the `.coverage`/htmlcov artifacts.

### xdist + pytest-bdd (compatible)

`-n auto --dist worksteal` coexists with pytest-bdd collection and pytest-cov:
the full suite runs green across parallel workers with coverage combined. **No
need to run BDD serially.** (For a single-file debug loop use `-n0`.)

### Reuse conventions

- **`.feature` files live beside their collector module** under `test/<area>/`
  (e.g. `test/storage/bdd/storage.feature` + `test/storage/test_storage_bdd.py`).
- **Collector `test_*.py` calls `scenarios("area/name.feature")`** and defines
  the steps it needs. `python_files = ["test_*.py"]` is unchanged.
- **`test/conftest.py` is the fixture registry** (§5) — shared by every area.
- There is **no shared step library** (no cross-area step definitions); each
  collector defines the steps it needs. Area-level _scaffolding_ may be shared
  through a plain helper module beside the collectors — today:
  `test/clients/hh/helpers.py` (HH client factory, auth-state seeding,
  `StubAuthInteraction`, `RecordingSolver`, `ScriptedResponses`, canned JSON
  payload TypedDicts + builders, `PNG_IMAGE`) and
  `test/clients/hh/browser_fake.py` (`FakeBrowserDriver`). These are pytest
  fixtures/doubles, not steps; features never import from another area.

---

## 9. Test areas (current layout)

`test/` is organized by domain area, each with a `test_*.py` collector; areas
whose behavior is expressed in Gherkin add a `bdd/` subdir beside the collector:

| Area           | Files / features                                                                                                                                                                                                                                                                                                                              |
| -------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --- |
| `families/`    | marker smoke suites: `test_unit_smoke`, `test_integration_smoke`, `test_bdd_smoke` (+`bdd/acceptance_smoke.feature`), `test_e2e_gate` (`test_qt_smoke` + `smoke_root.qml` removed with the UI)                                                                                                                                                |
| `harness`      | `test_harness_smoke.py` at `test/` root — proves the shared fixtures (§5)                                                                                                                                                                                                                                                                     |
| `storage/`     | `test_db`, `test_migrations`, `test_storage_bdd` (+`bdd/storage.feature`), `builders`                                                                                                                                                                                                                                                         |
| `config/`      | `test_config`, `test_cap_validation`, `test_config_bdd` (+`bdd/config.feature`), `helpers`                                                                                                                                                                                                                                                    |
| `stages/`      | `test_fetch`, `test_score`, `test_generate_cv`, `test_limits`, `test_prompts`, `test_apply`, `test_hh_test_apply_bdd` (+`bdd/*.feature`)                                                                                                                                                                                                      |
| `hh_tests/`    | `test_hh_tests_bdd` (+`bdd/hh_tests.feature`) — hh screening-test contract, prompt, AI + file solvers, selector, dump                                                                                                                                                                                                                         |
| `engine/`      | `test_engine`                                                                                                                                                                                                                                                                                                                                 |
| `clients/`     | `clients/mock/test_mock_client`; `clients/hh/` — six BDD collectors (`test_{authentication,applications,resumes,captcha,vacancy_search,vacancy_listing}_bdd` + `bdd/*.feature`), shared `helpers.py`, `browser_fake.py`, plus plain suites (`test_transport`, `test_search_params`, `test_filter_contract`, `test_captcha_debug_image`)       |     |
| `contract/`    | `test_factory`, `test_fake_conformance`                                                                                                                                                                                                                                                                                                       |
| `fakes/`       | `fake_client` (shared test double)                                                                                                                                                                                                                                                                                                            |
| `integration/` | `test_pipeline_flow` (+`bdd/pipeline_flow.feature`)                                                                                                                                                                                                                                                                                           |
| `ui/`          | **removed** with the UI implementation (`test_app`, `test_dashboard`, `test_edit_dialog`, `test_vacancy_detail`, `test_vacancy_manager`, `test_pipeline_manager`, `test_limit_manager`, `test_command_builder`, `test_qml_table`, `test_generic_table_model`, `conftest.py`); the rewrite (`docs/ui/jobfucker.ui.md`) will re-create the area |
| `cli/`         | `test_cli`                                                                                                                                                                                                                                                                                                                                    |
| `captcha/`     | `test_terminal`, `test_selector`, `test_captcha_ai`                                                                                                                                                                                                                                                                                           |
| `bootstrap/`   | `test_bootstrap`                                                                                                                                                                                                                                                                                                                              |
| `ai/`          | `test_ai`                                                                                                                                                                                                                                                                                                                                     |
| `runtime/`     | `test_runtime`                                                                                                                                                                                                                                                                                                                                |

---

## 10. Storage layer & DB schema-creation decision

Two DB-schema creation paths exist and **both are used**, deliberately:

- **Repository CRUD tests** (`test/storage/test_db.py`) build schema via the
  ORM metadata fast path — `await create_schema(engine)` (i.e.
  `Base.metadata.create_all` via `run_sync`) — via the **`storage` fixture** added
  to `test/conftest.py` (§5). It is fast (a fresh in-memory async engine per
  test) and is the first class of coverage for repository behavior.
- **Migration tests** (`test/storage/test_migrations.py`) build schema via the
  **Alembic migration path** (`alembic upgrade head`, driven programmatically
  through `storage.db.apply_migrations`) against a real **file** DB in
  `tmp_path` (not in-memory — Alembic opens its own pooled connection). This
  proves the migrations produce exactly the tables the models declare (the
  canonical, integration-faithful path the composition root uses).

**Decision:** the migration path is authoritative for matching the frozen DDL;
`metadata.create_all` is used purely as a speed shortcut for the high-frequency
repository tests. Both are verified to agree
(`test_migrated_schema_matches_orm_models`).

Other conventions:

- **SQLAlchemy boundary is enforced.** `[tool.ruff.lint.flake8-tidy-imports
.banned-api]` bans `sqlalchemy`; the only permitted importers are
  `src/jobfucker/storage/**` and `test/**` (via `TID251` per-file-ignores).
  Engine/CLI/UI consume only frozen DTOs. Verify with
  `grep -rn "sqlalchemy" src --include=*.py | grep -v "/storage/"`.
- **Migrations dir is excluded** from basedpyright/ruff (Alembic-generated
  tooling). Repos/DTOs/`db.py` are type-checked strict.
- `storage` fixture returns a `Storage` facade (async engine + session factory +
  the five repositories) over a fresh in-memory engine with schema created.

---

## 11. Open risks (accepted, tracked)

- **pytest-bdd upstream warnings.** Installed pytest-bdd 8.1.0 triggers
  `PytestRemovedIn10Warning` under pytest 9 (filtered in pyproject
  `filterwarnings`); the `gherkin-official` dep also emits a `DeprecationWarning`
  (`maxsplit` positional). Both are filtered; revisit when upgrading pytest.
- **`asyncio.iscoroutinefunction` deprecation** (from asyncio/pytest-asyncio) is
  filtered; it surfaces via the qasync/pytest-asyncio boundary and should be
  revisited when those dependencies change.
- **Parallel-Qt flakes (dormant).** Native offscreen Qt (`QQuickView`) occasionally
  segfaulted a worker when a Qt-only subset ran with `-n ≥ 2`. Qt tests are removed
  today; if the rewrite's Qt tests flake under xdist, prefer `-n0 -m qt` or a
  qt-serial runner.
