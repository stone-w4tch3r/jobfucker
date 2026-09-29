# Known bugs

Defects found while building the Habr client but living in **core** (not the
`clients/habr` package). Recorded for later handling.

## 1. `vacancies` storage drops `key_skills` (and `area`, `published_at`)

- **Where:** `src/jobfucker/storage/models.py` (the `vacancies` table has no such
  columns) / the row mapping in `src/jobfucker/storage/db.py`.
- **Symptom:** the client maps `Vacancy.key_skills`, `area`, `published_at`, but the
  stored row keeps only a subset — `key_skills` never reaches the DB, so a re-score /
  backfill that reads from storage cannot see structured skills.
- **Impact:** board-neutral; identical for HH and Habr. Visible only via
  `vacancies dump` / direct DB inspection.
- **Evidence:** live Habr `fetch` + `vacancies dump` (2026-09-29): 40 rows, full
  descriptions present, no `key_skills` surface.
- **Suggested fix:** persist `key_skills` (JSON/text column), or drop it from the
  contract mapping if nothing downstream uses it.

## 2. `--force` re-apply of an already-applied vacancy rewrites the local ledger

- **Where:** apply-stage ledger write (`src/jobfucker/stages/apply.py`) + the CLI
  `--force` semantics.
- **Symptom:** re-running `apply --vacancy-id <applied> --force` makes the client
  preflight return `ApplySkipped(already_applied)`; the stage records the outcome as
  `declined`, overwriting the existing `applied` status (clearing `applied_at`), while
  the board still shows it applied.
- **Evidence:** live Habr run (2026-09-29): `[1/1] declined … was already applied to`,
  0 POSTs, local `status` then `1 applied · 1 declined`.
- **Note:** without `--force` the "decided" gate normally prevents re-processing, so it
  is reachable only via forced re-runs.
- **Suggested fix:** treat `already_applied` as a no-op that preserves the prior
  terminal outcome, or have `--force` skip vacancies whose board state is already
  `applied`.
