# AI Guide — jobfucker

**Authoritative reference for AI agents.**

---

## Doc map (one fact, one home)

| Fact kind | Home doc |
| --- | --- |
| Agent entrypoint, subsystem status, commands, verification | **this file** |
| Product goal, user-level behavior, stage semantics | [docs/specs/jobfucker.md](docs/specs/jobfucker.md) |
| System shape: layers, modules, data model, invariants | [docs/specs/architecture.md](docs/specs/architecture.md) |
| Board-neutral Client interface | [docs/specs/client-contract.md](docs/specs/client-contract.md) |
| HH client requirements | [docs/specs/hh-client.md](docs/specs/hh-client.md) |
| Observed hh.ru behavior (endpoints, captcha, auth) | [docs/hh/](docs/hh/README.md) |
| Code standards | [docs/coding_rules.md](docs/coding_rules.md) |
| Feature overviews | [docs/features/](docs/features/) |
| GUI future plan | [docs/ui/jobfucker.ui.md](docs/ui/jobfucker.ui.md) |

Link to a home doc; never duplicate its facts.

**Reading path:** new to project → this file → product spec → architecture spec. Client work → client-contract → hh-client → docs/hh/.

---

## Critical Reading Path

1. **New context?** Read this file fully and load `engineering-principles` skill.
2. **Spec authority:** product, architecture, client-contract, hh-client specs (see doc map). `docs/hh/` is the canonical reference for observed HH.ru behavior.
3. **Implementation phase?** Consult [coding_rules.md](docs/coding_rules.md).

**Invariant:** `uv run poe lint_full` + `uv run poe test` is the standard verification flow. Run lint continuously during development, not only at finalization.

**Command policy:** run project-local Python tools through `uv` (`uv run python`, `uv run pytest`, `uv run ruff`, `uv run basedpyright`, `uv run poe`, `uv run pre-commit`), not system binaries.

### Skills — ALWAYS CHECK, ALWAYS USE

<EXTREMELY_IMPORTANT>

**BEFORE writing ANY code, ALWAYS check available skills and USE every skill that matches your scope.** Code ignoring them WILL fail review. When delegating to subagents, tell them which skills to use.

- `architecting-python-changes` — ALWAYS load when planning a feature, non-trivial fix, or refactor affecting structure, layering, wrappers, or library choice.
- `writing-python-code` — ALWAYS load when writing/editing Python.
- `testing-python` — ALWAYS load when writing tests or fixtures.
- `setting-up-python-projects` — ALWAYS load when bootstrapping a new package/app.
- `building-multi-ui-apps` — DO load — jobfucker ships a CLI and plans a Qt GUI sharing one domain core.
- `building-qt-apps` / `setting-up-shortcuts` — DO load when the planned GUI rewrite starts (PySide6/QML code, keyboard shortcuts).
- `setting-up-logging` — DO load when adding or changing logging.
- `writing-scripts` — DO load when creating standalone probe/dev scripts.

</EXTREMELY_IMPORTANT>

---

## Project Overview

Vacancy application automation engine for Russian job boards (initially hh.ru). Own HTTP-only HH client, AI scoring + cover-letter generation, local SQLite DB, daily-apply-limit tracking. Typer CLI (`jobfucker`). GUI planned for re-implementation ([plan](docs/ui/jobfucker.ui.md)).

**Subsystem status (single source of truth):**

| Subsystem | Status |
| --- | --- |
| Engine, stages (fetch/score/generate/apply), storage, config, limits, audit | **real**, BDD-tested |
| Mock client (offline, scriptable) | **real** |
| HH client: transport, auth, captcha (browser engine + login protocol), search + detail enrichment | **real**, BDD-tested vs mock transport + scripted browser double |
| HH client: apply (preflight, submission, reconciliation) | **real**, BDD-tested vs mock transport |
| HH client: resumes listing + pre-apply resume validation | **real**, BDD-tested vs mock transport |
| HH client: screening-test fetch + website apply-with-answers (`HhTestCapable`) | **real**, BDD-tested vs mock transport |
| Screening-test solving (AI + answers file, `hh-tests dump`, `apply --test-answers`) | **real**, BDD-tested |
| Vacancies review/edit CLI (`jobfucker vacancies dump\|apply\|edit`) | **real** |
| Search preview (`jobfucker search`, listing-only `Client.list_vacancies`) | **real**, BDD-tested |
| Qt GUI | **removed**; planned rewrite per [ui spec](docs/ui/jobfucker.ui.md) |

**Key decisions:**

- **Client contract layer** — engine/stages/storage never depend on a board; `Client` protocol + vendor-neutral models in `clients/base.py`. New board = new client under `clients/<board>/` + factory registration.
- **Own SQLite DB** (`jobfucker.db`) as single source of truth.
- **Captcha is first-class & generic** (`captcha/`): handler selection, terminal rendering (sixel/kitty), AI vision solving. HH-specific detection stays in the HH client; standalone challenges are cleared by the browser engine (`clients/hh/browser.py`, patchright stealth Chromium — the only working path, see [docs/hh/captcha.md](docs/hh/captcha.md) §5a); the embedded login challenge keeps the browserless multipart replay.
- **GUI:** PySide6 toolchain kept in deps for the planned rewrite; `shared/shortcuts` needs PySide6 today.

---

## Toolchain

| Tool | Purpose |
| --- | --- |
| Python 3.14+ | Runtime |
| `uv` | Package management, script execution |
| `basedpyright` | Type checking (strict, `reportAny=error`) |
| `ruff` | Strict linting + formatting |
| `pytest` + pytest-bdd | Testing |
| `poethepoet` | Task runner |
| `rusty-results` | Result pattern for error handling |

---

## Coding Standards (summary — full rules in [coding_rules.md](docs/coding_rules.md))

### Error Handling (Result Pattern)

- Expected failures (IO, network, user input): return `Result[T, E]`; every `Client` method returns `Result[T, ClientError]`.
- Programming errors: raise (fail-fast).
- At third-party boundaries: catch immediately, wrap in `Result`.

### Type Safety

- Strict mode. No `Any`. No `typing.cast()`.
- pydantic models at external boundaries (config, API payloads); dataclasses for domain objects.
- Wrap untyped third-party boundaries (`openai`, HTTP payloads) with typed interfaces + runtime validation.

### Async

- Whole domain is async (engine, stages, clients, storage: SQLAlchemy `AsyncEngine` + `aiosqlite`).
- CLI: each command wraps core work in `asyncio.run(...)` (one-shot loop).
- BDD steps stay synchronous, drive async domain via `jobfucker.testing.step_runner` (pytest-bdd cannot await `async def` — async steps silently no-op → false green).

### Cross-platform (Windows / POSIX)

- Windows, macOS, and Linux are equal runtime targets; code works on all of them.
- Default to OS-agnostic implementations, eg `pathlib`, no POSIX-only imports/syscalls, per-OS directories and so on.
- Small platform differences (a chmod call, an editor default) may be compact `sys.platform` / `os.name` branches inline — kept narrowable so the type checker validates each side.
- A complex OS-specific subsystem is a distinct service behind a protocol with one implementation per OS — never scattered conditionals.
- Enforcement: basedpyright infers the platform per machine; CI runs lint + tests on Linux and Windows.

---

## Architecture

See [architecture spec](docs/specs/architecture.md). Short: presentation (typer CLI) → domain (engine, stages, app services) → infrastructure (clients, storage, captcha). Dependencies flow downward only; core never imports a board; generic names (`external_id`) not board specifics.

---

## Key Workflows

| Goal | Command |
| --- | --- |
| Type check + Ruff + custom linters | `uv run poe lint_full` |
| Run tests | `uv run poe test` |
| CLI help | `uv run poe app -h` (`app` = `python -m jobfucker` = `jobfucker`) |
| Pre-commit verification | `uv run poe lint_full && uv run poe test` |
| Captcha browser engine install | `uv run poe browser-install` (patchright Chromium; required before first captcha solve) |
| Register pre-commit hooks | `uv run pre-commit install` |

---

## CLI Commands (verified against `-h`)

```bash
jobfucker init      --config FILE            # parse + validate + cap check + store pipeline
jobfucker update    --config FILE            # append config snapshot under existing identity (id stays stable)
jobfucker run       (--pipeline-id | --new-from-config)  # fetch → score → generate → apply
                    [--from/--to/--take/--skip-already-processed]      # batch stages (id-ordered offsets)
                    [--first-page/--page-size/--take-pages]             # fetch window; ANY given flag = run-wide override of every pool entry's window
jobfucker fetch     --pipeline-id [--use-search-config N] [--refresh]   # walk the pool of service.<board>.searches[] (insert-only), one mirror sync per entry
                    [window/slice flags]                                # run-wide override: any given flag replaces every entry's configured window
jobfucker search    --pipeline-id --use-search-config N [--query] [--params FILE|-|'{...}']   # listing-only preview: nothing stored
                    [--format text|json|yaml] [window flags]            # entry query+filter as base; --use-search-config required when pool > 1
jobfucker score     --pipeline-id [batch flags]
jobfucker generate  --pipeline-id [batch flags]
jobfucker apply     --pipeline-id [batch flags] [--use-sixel|--use-kitty|--no-captcha-ai]
                    [--min-score N] [--include-unscored] [--allow-without-letter]  # per-run eligibility relaxations
                    [--vacancy-id ID ...] [--force]   # forced ids: bypass filters (--force: also decided gate)
                    [--has-hh-tests any|only_with_hh_tests|only_without_hh_tests]  # hh test flag filter
                    [--test-answers FILE] [--no-test-ai]   # hh screening-test solving (AI default)
jobfucker resumes   --pipeline-id [--use-sixel|--use-kitty|--no-captcha-ai]   # list owned resumes (id, updated, title)
jobfucker hh-tests dump --pipeline-id [batch flags] [--output FILE]           # hh screening tests of eligible
                    [--has-hh-tests ...] [--min-score N] [--include-unscored] [--allow-without-letter]
                    # vacancies -> problems JSON (no application); solve offline, feed apply --test-answers
jobfucker status    [--pipeline-id]          # per-pipeline action buckets (need-scoring/need-generate/ready-to-apply/
                                            #   below-threshold/paused/stale + applied/declined/failed ledger)
jobfucker pipelines-list [--snapshots]       # identities (+ snapshot history)
jobfucker vacancies                          # review/edit subsystem:
    dump   [--output F] [--format yaml|json] [--force] [--pipeline-id]   # default: all pipelines (incl. soft-deleted)
    apply  [--source F|stdin] [--format] [--dry-run]     # document-driven; no pipeline flag
    edit   [--pipeline-id]                               # dump → $EDITOR → preview → confirm → apply
```

Global flags: `--log-level L`, `--log-stderr`. Captcha flags on fetch/apply/run.

---

## Manual Testing (offline, via the mock client)

The `mock` client runs board-facing behavior — CLI stages and captcha rendering — with no live board. Scoring and
cover-letter generation still use the configured AI boundary (tests inject an offline AI double).

**Isolate state:**

```bash
export CONFIG_DIR=/tmp/jf-test-config DATA_DIR=/tmp/jf-test-data
```

Env vars: `CONFIG_DIR` (config files), `DATA_DIR` (cookies, tokens, `jobfucker.db`), `JOBFUCKER_DB_URL` (alembic only). Deleting a dir resets what it holds.

**Demo run** — `docs/examples/pipeline.mock.yaml` contains 3 canned vacancies, scripted apply outcomes, and an offline
captcha on apply:

```bash
uv run poe app init --config docs/examples/pipeline.mock.yaml
uv run poe app fetch --pipeline-id <id>     # id printed by init
uv run poe app score --pipeline-id <id>
uv run poe app generate --pipeline-id <id>
uv run poe app apply --pipeline-id <id> --use-sixel   # or --use-kitty
```

Captcha rendering waits for typed input — the mock accepts any answer. EOF/Ctrl-C on a challenge marks that vacancy `error`, batch continues.

**Mock presets** — programmed in the `service.mock` yaml section (never read by core):

- `vacancies` — canned search results (omit → empty).
- `behavior.authorize_error` — string makes `authorize` fail.
- `behavior.default_apply` / `behavior.per_vacancy` — script apply outcomes: `applied | skipped | error | limit_exceeded` (+ optional `message`). `limit_exceeded` = daily-cap STOP signal.
- `behavior.captcha` — offline captcha on `authorize | search | apply`, `attempts` challenges.

---

## Ambiguity Resolution

If requirements are unclear, halt and request clarification from the developer.
