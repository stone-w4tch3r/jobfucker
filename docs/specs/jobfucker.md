# Product Spec — jobfucker

**What the product does and how it behaves for the user.**

System shape (layers, modules, DB) lives in [architecture.md](architecture.md). The board-neutral
interface lives in [client-contract.md](client-contract.md). Observed hh.ru behavior lives in
[docs/hh/](../hh/README.md). GUI future plan lives in [ui/jobfucker.ui.md](../ui/jobfucker.ui.md).

## Objective

`jobfucker` is a vacancy application automation tool for Russian job boards (initially hh.ru): fetch
vacancies for a pipeline's search pool, AI-score them against the user's resume (1–5), generate cover letters
for good matches, apply via the board client, and track daily apply limits. Shipped as a typer CLI.
A Qt GUI is a planned future surface ([plan](../ui/jobfucker.ui.md)); it will read/write the same
SQLite DB, never replace the CLI engine.

The user is a job seeker who wants to:

1. Fetch vacancies for every search of the pipeline's search pool (`service.<board>.searches[]`)
2. Score each vacancy against their resume using AI
3. Keep vacancies + scores + letters in a local DB
4. Generate a cover letter for each vacancy above the score threshold
5. Apply to vacancies via the HH client, solving employer screening tests when present (see [hh-client.md](hh-client.md))
6. Respect configured daily application limits; HH's `limit_exceeded` response is authoritative
7. Review and manually edit pipeline results (CLI `vacancies` workflow; GUI later)

A board is added later by implementing the `Client` contract as a new client — no engine, storage,
or UI changes (see [client-contract.md](client-contract.md)).

## User flows

**Basic loop:**

```bash
jobfucker init --config pipeline.yaml    # one-time: parse, validate, cap-check, store
jobfucker fetch --pipeline-id N          # walk the pipeline's search pool into the DB (insert-only)
jobfucker score --pipeline-id N          # AI scoring
jobfucker generate --pipeline-id N       # cover letters for score >= min_required_score
jobfucker apply --pipeline-id N          # apply to eligible vacancies (screening tests solved when present)
jobfucker vacancies dump | yq/jq | $EDITOR | jobfucker vacancies apply   # human review/edit
```

`run` chains fetch → score → generate → apply in one command. Full command reference:
[AGENTS.md](../../AGENTS.md#cli-commands-verified-against--h). Config file reference:
[pipeline.example.yaml](../examples/pipeline.example.yaml).

**Query-iteration flow (`search`):** preview what the board returns for the pipeline's search —
listing only, **nothing stored** (no vacancy bodies, no DB writes, no audit). The pool entry is
selected with `--use-search-config N` (mandatory when the pipeline has more than one search —
fails fast with the count otherwise); `--query` overrides that entry's query and
`--params FILE|-|'{...}'` overrides its board's whole filter block (same schema as the entry's
`filter`; validated locally — unknown keys/bad values fail before any board call). The pipeline's
per-entry `window` is **not** read by `search` — its window flags keep their own defaults. Output
shows the board-reported `found`
total, a title-first table (title, company, salary, area, published) with a per-vacancy **DB
status** word (`new` = not stored, else `fetched`/`scored`/`lettered`/`pending`/`applied`/
`skipped`/`error`/`manual-skip`/`deleted` — most decisive state wins; soft-deleted rows read as
`deleted` because fetch silently skips them), and the board's own web search URL for the query.
`--format text|json|yaml` switches the rendering; window flags (`--first-page`/`--page-size`/
`--take-pages`) reuse the fetch semantics. This is the iterate loop: tweak → search → read →
repeat, then `fetch` once the query is right (the pipeline stores the tuned set as a pool entry).

**Review/edit flow (`vacancies`):** `dump` writes every vacancy (all pipelines by default, incl. soft-deleted;
`--pipeline-id N` scopes the document to one pipeline) as a YAML/JSON document — read-only context (external id,
listing, staleness flags), editable block (score, score_reasoning, cover_letter, notes, manual_skip,
manual_skip_reason, deleted). `apply` validates against the JSON Schema, applies present editable fields verbatim (last-writer-wins; read-only context edits are ignored), in one transaction, writes an audit entry; it is document-driven
and has no pipeline flag. `edit` = dump → private tempfile → `$EDITOR` → preview → confirm (accepts
`--pipeline-id`). Removed rows from a doc just shrink the batch — never a deletion; soft-delete is the explicit
`deleted` flag. An unknown or soft-deleted `--pipeline-id` fails with `Pipeline id N not found.`

## Stage semantics (behavior)

**fetch** — windowed listing sync. `--first-page`/`--page-size`/`--take-pages` select the window
of the listing to load; `--from`/`--to`/`--take` select the slice to persist (0-based positions
from page 0; `--to` excludes `--take`; `--first-page` excludes `--from`/`--to`). All combinations
are validated before any network call, including window ≤ board max (HH: 2000). Pagination and
trimming happen client-side: only overlapping native pages are walked, only slice items get detail
calls, already-stored ids are excluded client-side. Persistence is **insert-only**: new ids
inserted, existing rows untouched (processed results survive verbatim); `--refresh` overwrites
**listing fields only** (dirty-check skips no-op writes so `fetched_at` doesn't bump falsely).
Soft-deleted rows are never touched by fetch. Disappearing from the listing deletes nothing.
A mid-walk page failure persists everything loaded and errors with the failed page + resume hint.

**score** — renders the scoring prompt (Jinja2, context = resume + formatted vacancy) and asks the
AI for structured `{fit_score 1..5, comment}`. Sub-threshold (`score < min_required_score`) is a
derived read, never a stored state — `apply_status` is not written (it describes board-side apply
outcomes only). Per-item failure = `score_error` field; the batch never aborts on item errors.

**generate** — only vacancies with score ≥ `min_required_score`; plain-text cover letter accepted
verbatim; same per-item error handling.

**apply** — eligible = has cover letter + score ≥ min + not manual_skip + not already
applied/skipped/errored. Checks the daily limit before each apply (see Limits). Limit stop leaves
the rest pending (Ok report, `limit_reached`); upstream `limit_exceeded` does the same; fatal
errors that retries cannot help — `ConfigurationError` (e.g. the board rejected the configured
resume) and `AuthError` — stop the batch the same way; other per-item apply failures set
`apply_status='error'` and continue.

Per-run relaxations (`jobfucker apply` flags, config untouched): `--min-score N` overrides the
threshold for the run; `--include-unscored` admits unscored vacancies; `--allow-without-letter`
admits letterless vacancies (no message is sent — the client contract supports letterless apply);
`--vacancy-id ID` (repeatable) restricts the run to exactly those ids and bypasses the
score/letter/manual-skip filters for them; `--force` (ids only) also bypasses the decided gate,
re-attempting previously decided vacancies. Daily limits apply in every mode.

**Screening-test solving (hh):** when a vacancy carries an employer test (fetch recorded
`has_hh_test` true), the apply stage fetches the test, solves the whole test, and
submits the answers with the application. Solver selection: `--test-answers
FILE` uses a file of answers (produced by `hh-tests dump` → edit/solve offline); otherwise the AI
solver from the optional top-level `hh_test_solving` config section runs, unless `--no-test-ai` is
given. `--has-hh-tests any|only_with_hh_tests|only_without_hh_tests` filters the batch by the
stored flag. A missing or failed solve
marks that vacancy `error` and the batch continues; when the section sets
`allow_ai_to_skip_test_when_not_enough_context`, the AI solver may instead decline the test with
a comment, and that vacancy is marked `skipped` (comment stored as the skip reason, never a
failure). A test removed between fetch and apply falls
back the plain apply. Tests are hh-only — other boards have no such capability.

**Idempotency:** stages are repeatable per pipeline. `--skip-already-processed` is **stage-relative**:
it skips rows that already carry *that stage's* result — a `score` for `score`, a `cover_letter`
for `generate`, an apply decision (`apply_status` set) for `apply`. `apply_status` is the apply
stage's exclusive vocabulary: the score stage never stamps it, so a scored-but-letterless vacancy
stays a `generate` candidate and a scored-but-never-applied one stays an `apply` candidate.
`--from/--to/--take` are offsets into the id-ordered vacancy list.
Soft-delete is a **manual** lifecycle action (via `vacancies`), never triggered by any stage.

**Pipeline lifecycle:** `init` is get-or-keep by name. `update` appends a new config snapshot under
the same identity — nothing is soft-deleted, the id never changes. Every content slot (login,
password, resume, api keys, prompts) is xor file-reference/inline; file contents are read once at
init/update, stored in the snapshot, and never re-read later.

## Limits

**TBD — the semantics are not settled.** The current implementation is corrupted by design drift:
the `Limits` API models two counters (per-auth, per-pipeline) but the DB and callers have **one**
shared count per `(service, login, date)`, checked against both caps (client safety cap + pipeline
`daily_apply_limit`); effective stop = tighter cap. Proper per-pipeline semantics — if wanted — is
a future decision. Not documented as final until settled.

Unchanged regardless of resolution: each `apply` increments today's count on `ApplySucceeded`; the
count stops the batch before the cap; an upstream `limit_exceeded` response is authoritative and
stops the run even below local caps. Counters are shared across all pipelines on the same login —
a breakage if the user also applies via the website (known limitation).

## Captcha behavior

Captcha is first-class and detected generically at the client boundary — every response is checked
for challenges before status mapping. Solvable challenge types are solved over raw HTTP by the HH
client (see [hh-client.md](hh-client.md)); an exhausted/unsolvable challenge returns a typed error
carrying a **recovery URL** — the human opens and solves it, the job resumes on a later run. No
browser is launched, no infinite retry loops.

Handler selection (generic `captcha/` module): explicit `--use-sixel`/`--use-kitty` wins; else AI
vision solving if `openai_captcha` is configured (independent section — never falls back to the
main `openai` config) and `--no-captcha-ai` is not set; else terminal auto-detection; else fail-fast
with instructions. The AI handler fires `openai_captcha.consensus_requests` (K, default 4)
concurrent vision requests per attempt and submits the most-common (plurality) answer; a wrong
consensus is retried on a fresh image within `service.hh.captcha_max_attempts`. The vision prompt
carries a non-binding tip about the typical answer shape (lowercase Cyrillic, word-like, single
spaces); the image always wins over the tip. Manual interactions (one-time codes) flow through an injectable
interaction provider, never raw `input()` in core code.

## Runtime isolation

Session cookies, tokens, and the DB live under platform data dirs, overridable via `CONFIG_DIR` and
`DATA_DIR`; deleting a dir resets what it held (details: [architecture.md](architecture.md)).

## Boundaries

- All board I/O goes through the `Client` contract; the engine never calls board APIs directly.
- All AI calls (scoring, letters, captcha vision) go through `ai.py`'s typed wrapper.
- No daemon; every command is one-shot. No web crawling; batch stages only.
- Only the CLI mutates engine-managed data today; the future GUI edits user fields through the same
  DB and logs to `audit_log` ([plan](../ui/jobfucker.ui.md)).

## Testing

Everything runs offline against the first-party mock client (scriptable behaviors, offline captcha);
the HH client is tested against a mock HTTP transport with real client code; BDD features cover
config, storage, pipeline flow, stages, and HH auth/captcha/search. Breakdown:
[architecture.md](architecture.md#testing-shape).

## Success criteria (CLI product)

1. `init` parses a valid `pipeline.yaml` (xor slots enforced, cap pre-checked) and stores it.
2. `run` fetches, scores, generates letters, and applies to eligible vacancies above the threshold;
   vacancies below `min_required_score` end up `skipped`, never applied.
3. Daily limits are respected — stops at the effective cap; `limit_exceeded` stops even below it.
4. Re-fetch never destroys processed state; `--refresh` updates listing fields only.
5. Standalone stage runs with `--skip-already-processed` do not redo processed vacancies.
6. `vacancies dump → edit → apply` round-trips a document; present editable fields overwrite the current
   database values (last-writer-wins) and every apply writes an audit entry.
7. Captcha challenges render in a capable terminal or are solved by AI; an unsolvable one surfaces
   a recovery URL and stops gracefully.
8. Screening-test vacancies are applied to with solved answers (AI or `--test-answers`); a failed
   solve marks only that vacancy `error` (or `skipped` when the AI-decline opt-in is set, with the
   AI's comment as the reason), and `--has-hh-tests` filters by the fetch-time flag.

GUI criteria will be defined with the GUI rewrite, not before ([plan](../ui/jobfucker.ui.md)).
