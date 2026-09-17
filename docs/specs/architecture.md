# Architecture — jobfucker

**System shape: layers, modules, data model, invariants.**

Authority rules: the ORM (`src/jobfucker/storage/models.py`) is the schema truth — no DDL is maintained in docs.
The CLI help (`jobfucker -h`) is the command truth. Product behavior lives in [jobfucker.md](jobfucker.md);
the board-neutral interface lives in [client-contract.md](client-contract.md).

## Layer map

```
PRESENTATION      cli.py (typer) · cli_commands/vacancies.py        — thin shell
                                   ↓
APPLICATION       engine.py · stages/ · app/ · table_documents/ · hh_tests/
                  config.py · ai.py · reporting · limits · runtime · bootstrap
                                   ↓
CLIENT CONTRACT   clients/base.py (protocol + models) · factory · paging
CLIENTS          clients/hh/ · clients/mock/
                                   ↓
INFRASTRUCTURE    storage/ (SQLAlchemy + alembic) · captcha/ (board-agnostic)
                  src/jobfucker/shared/ (logging; shortcuts — GUI residue)
                  tools/ (linting — dev-only, not in the wheel)
```

- Dependencies flow downward only. SQLAlchemy is confined to `storage/` (ruff `TID251` ban).
- Core never imports a board in generic code; generic names (`external_id`, `service`) everywhere for features.
- If a feature is board-specific, it should be hidden behind a capability and special code flow, not behind random if-s. The only case when board specific naming can reach the core is when there is a dedicated capability associated. Example:  `Vacancy.has_hh_test` (`vacancies.has_hh_test` + `apply --has-hh-tests`); board-only
  behavior rises into core through a board-named capability module gated by `isinstance`
  ([client-contract.md § Board-scoped capabilities](client-contract.md#board-scoped-capabilities)).
- Purple-flagged residue: Qt toolchain deps + `src/jobfucker/shared/shortcuts` were removed and are re-added with the planned GUI rewrite; `tools/linting` is dev-only scaffold tooling (not shipped).

## Module inventory (what each thing actually does)

| Module | LOC | What it does |
| --- | --- | --- |
| `cli.py` + `cli_commands/` | ~1,031 | Thin typer shell. Each command: parse → `asyncio.run` → open `AppServices` (close in `finally`) → `build_engine_from_pipeline` → engine → print report. `CliReporter` prints live `RunEvent`s filtered by `-v/-vv`. |
| `engine.py` | 367 | Composition root per run: storage + client factory + config + AI + snapshot_id + reporter. Methods `fetch/score/generate_cv/apply/run`. Fresh client per stage call, closed in `finally`. |
| `stages/` | 1,634 | fetch, fetch_plan, score, generate_cv, apply, prompts. Free functions: storage + pipeline + inputs → frozen report. |
| `config.py` | 735 | pydantic config models; xor file/inline slot validation for every content slot; per-board section registry; snapshot serialization round-trip; `reconstruct_pipeline_config` (zero file I/O). |
| `app/` | ~1,100 | `services.py` AppServices (CLI-utility graph; its factory builds no client); `pipeline_service.py` (create/get-or-keep/snapshot/resolve — the lifecycle core); `mapping.py` (config↔DTO); `vacancy_documents.py` + schema (dump/apply policy: 7 editable fields, identity from handle, plain field overwrite). |
| `table_documents/` | ~310 | Generic YAML/JSON document codec + orchestration; one consumer (vacancies). |
| `clients/base.py` | 398 | `Client` protocol, vendor-neutral models, 10-member `ClientError` union, handler seams, `ServiceConfigSection`. |
| `hh_tests/` | ~690 | Board-scoped screening-test capability: `HhTestCapable` protocol + models + validation, prompt rendering, AI + file solvers, selector, dump serializer. `__init__.py` is import-free to break the config↔factory↔hh_tests cycle. |
| `clients/hh/` | ~2,490 | Real client: transport, auth, captcha, search, resumes, apply, screening-test web flow. Implements `Client` + `HhTestCapable`. |
| `clients/mock/` | 627 | Scriptable offline client, self-configured from its yaml section. |
| `clients/paging.py` | — | `plan_pages` (pure math) + `scan_slice` (walk; short page = exhausted; page failure = partial result, never bare Err). |
| `captcha/` | 658 | Handler selector, sixel/kitty encoders, terminal capability map, terminal + AI handlers. Board-agnostic. |
| `ai.py` | 532 | `OpenAIWrapper`: structured-output scoring `{fit_score 1..5, comment}`, cover letters, 3-attempt retry (network + validation), JSON-extraction tolerance, injected `CompletionsFn` seam. |
| `storage/` | ~1,512 | ORM models (schema truth), frozen DTOs, repositories, atomic snapshot ops, migrations runner. |
| `bootstrap.py` | 266 | CLI-only engine rebuild facade — the **real** second composition root (builds its own factory with captcha handler). |
| `schema_generation.py` + resources | — | Generates the vacancy-doc JSON Schema; version = content hash. Kept. |
| `limits.py` | 61 | `Limits.can_apply(used, used)` — see Limits below. |
| `reporting.py`, `runtime.py` | 226 | `RunEvent`/`Reporter` stream beside logging; XDG `CONFIG_DIR`/`DATA_DIR` resolution. |

## Data model (summary — ORM is the authority)

Five tables (`storage/models.py`):

| Table | Shape |
| --- | --- |
| `pipelines` | **Identity only**: `id`, `name` (unique, active), `description`, `current_snapshot_id`, timestamps. No config. |
| `pipeline_snapshot` | **Append-only**, `snapshot_no` per pipeline. The whole `pipeline.yaml` as **content columns**: service + opaque `service_section` JSON (with the search pool), login/password/resume, openai fields (incl. `openai_captcha` JSON), prompts, `min_required_score`, `daily_apply_limit`. |
| `vacancies` | Listing fields + results (`score`, `score_reasoning`, `cover_letter`, `apply_status` CHECK, errors), `has_hh_test` (fetch-time flag), `manual_skip`(+reason), `notes`, `soft_deleted_at`, staleness timestamps (`fetched/scored/generated/user_edited_at`), **four provenance FKs** `*_snapshot_id`. Ordered by `id` (no position column). |
| `audit_log` | `pipeline_id`, `pipeline_snapshot_id`, `action`, `details` JSON. |
| `daily_limits` | `count` per **`(service, login, date)`** — UNIQUE, shared across all pipelines on the account. No `pipeline_id`. |

HH auth state lives outside SQL: `$DATA_DIR/hh/<sha256(login)>/auth-state.json` — tokens + cookies, 0600, atomic write (`TokenStore`).

## Invariants (explain most of the code)

1. **Errors are values.** Every boundary returns `Result[T, E]`; whole-batch `Err` (auth/transport/plan) vs per-item failure **fields** on rows (never aborts).
2. **The DB is the config.** After `init`, no command reads `pipeline.yaml` again; each stage run reconstructs a full `PipelineConfig` from the head snapshot with zero file I/O.
3. **Provenance.** Every artifact column records which snapshot produced it (`fetched/scored/generated/applied_snapshot_id`).
4. **Fetch is insert-only.** New ids inserted; existing rows untouched unless `--refresh` (listing fields only, dirty-checked). Soft-delete is manual, never set by fetch.
5. **One shared daily counter.** `daily_limits` counts per `(service, login, date)`; each apply checks that one count against both caps (client `per_auth_daily_cap`, pipeline `daily_apply_limit`). Semantics marked TBD in the product spec — see Limits there.

## Composition roots

- `bootstrap.py` — the real one for CLI runs (engine rebuild, captcha handler, client factory with a bound section).
- `app/services.py` `AppServices` — CLI utility graph (cap validation, status commands). Its factory has **no section bound** and can never construct a client; vestigial lean from the removed GUI. Kept as-is for now (user decision).

## Captcha subsystem

- **Selector order: flag → AI → autodetect → fail** (`captcha/selector.py`): explicit `--use-sixel`/`--use-kitty` wins; then `openai_captcha` config (unless `--no-captcha-ai`); then terminal detection; else fail-fast Russian message.
- **AI prompt is a hardcoded one-liner** in `captcha/ai.py` — no file, no config plumbing.
- Detection is generic at the client level: the HH client classifies challenges on **every** response before status mapping; unsolvable challenges collapse into `CaptchaSolvingError` carrying a `recovery_url` for the human-fallback flow.
- **Embedded login captcha is implemented and verified**: re-POST of the login form with `captchaKey/Text/State`, bounded attempts (`clients/hh/auth.py`, `clients/hh/captcha.py`). See [hh-client.md](hh-client.md).

## Testing shape

50+ test files: pytest-bdd features (config, storage, pipeline flow, stages, HH auth/captcha/search, hh screening tests, smoke) + contract tests vs in-memory fake + HH client vs `httpx.MockTransport` (real client code, no internal patching) + CLI e2e in isolated dirs + gated live-HH e2e (`--run-e2e`). BDD steps drive the async domain through `jobfucker.testing.step_runner` (tripwire test guards against silently no-op async steps).

## Known tensions (as-built, documented not solved)

- Dual composition roots (see above) — any future GUI must reconcile them.
- `Limits` API models two counters; DB + callers have one (`can_apply(used, used)`). Product spec marks semantics TBD.
- `vacancies dump/edit` accept an optional `--pipeline-id` (construction-time scope on the vacancy-document service); `apply` stays document-driven across all pipelines.
- Fetch does per-vacancy `get_by_external_id` + upsert (2 queries/vacancy). Fine at current scale.
