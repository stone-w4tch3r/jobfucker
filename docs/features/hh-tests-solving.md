# Feature — HH screening-test solving

**What this feature does for the user and how the pieces fit.** The verified hh.ru HTTP contract
lives in [docs/hh/tests.md](../hh/tests.md); the client side in
[hh-client.md](../specs/hh-client.md#employer-screening-tests); the boundary rule in
[client-contract.md](../specs/client-contract.md#board-scoped-capabilities). This page is the
feature-level overview, it does not restate those facts.

## Problem

Many hh.ru vacancies carry an employer screening test («Тест при отклике»). The old apply flow
skipped them (`has_test || test.required` preflight arm), so those applications were never made.
Also see ([tests.md §6](../hh/tests.md)).

## What ships

- Apply not skips test-bearing vacancies. When a vacancy was fetched with `has_hh_test`
  true, the apply stage fetches the test, solves the whole test via solver, and submits the answers together with the application in one website POST.
- Two solver paths:
  - **AI** (default when the `hh_test_solving` config section is enabled): one completion call per
    test, whole test in one prompt.
  - **Answers file** (`apply --test-answers FILE`): offline/async solving.
- `hh-tests dump` writes the unsolved tests of eligible vacancies to a JSON problems document
  (`vacancy_id -> {meta, name, description, tasks}`). A human or an offline AI pass fills the
  answers; `apply --test-answers FILE` consumes them.
- `--has-hh-tests any|only_with_hh_tests|only_without_hh_tests` narrows a run by the fetch-time
  flag.
- `--no-test-ai` disables the inline AI solver.
- `has_hh_test` is stored at fetch (from the search detail) and backfilled for older rows by
  migration.

## Config

Optional top-level `hh_test_solving` section:

```yaml
hh_test_solving:
  enabled: true
  allow_ai_to_skip_test_when_not_enough_context: false  # opt-in AI decline
  test_prompt_file: prompts/test.md   # or test_prompt: |
  # the user-authored prompt is the solver context; the test is appended
```

The AI solver reuses the main `openai` section (no separate credentials). A file solver ignores
the prompt. `allow_ai_to_skip_test_when_not_enough_context` is off by default.

## Semantics

The exact response classification is owned by
[hh-client.md](../specs/hh-client.md#employer-screening-tests) and the stage behavior by
[jobfucker.md](../specs/jobfucker.md); only the feature-level rules live here:

- answers-file solves offline/async; AI solves inline (one request per whole test);
- no solver, or a solver `Err`, on a **live** test → that vacancy is `error` (`apply_error`),
  batch continues;
- with `allow_ai_to_skip_test_when_not_enough_context`, the AI may return
  `{"not_enough_context_for_test": "<comment>"}` instead of answers → that vacancy is
  `skipped` (`skip_reason` = the AI comment, shown in the run output), batch continues;
- if a vacancy that used to have a test has no test during apply - fall back **once** to the plain apply.

## Scope

- hh-only: the capability is `HhTestCapable` in `jobfucker/hh_tests/`; the mock and future boards
  do not implement it, so the stage's `isinstance` gate excludes them by construction.
- Known ceiling: no DB persistence of solved answers — the answers file / dump document is the
  record.
- Known ceiling: the website popup requires a non-empty cover letter — a test-bearing vacancy
  applied with `--allow-without-letter` fails per-vacancy with `letter-required` (observed live).
