---
name: jobfucker-creating-scoring-prompts
description: >-
  Build and iterate jobfucker AI scoring.
  Use when creating a scoring prompt, calibrating AI scores against real vacancies, fixing
  over-/under-scoring, growing examples or anti-examples, or tuning apply/test prompts.
---

# Creating Jobfucker Prompts

Turn a resume + user requirements → a live, calibrated scoring prompt (Jinja2 template file).

## Rules

- **Every stage runs only on explicit user approval.** Stages are heavy (search, fetch) and interactive.
- **Stages are independent and resumable.** Run in order I→VI, but skip/repeat any stage; each stage's *Precondition* states what must already exist. Different chats may own different stages.
- **No fetch without approval.** Stages II–III are search-only. Fetch appears only in IV, explicitly approved.

## Engine contract

- Prompt file is plain text, rendered with **exactly two variables**: `{{ resume_formatted }}` and `{{ vacancy_formatted }}`. `StrictUndefined` is on → any other variable is a hard render error.
- Vacancy is text is prepared by the app and injected already formatted.
- Output contract is code-owned: strict JSON `{"fit_score": int 1..5, "comment": string}`. Never add schema keys.
- `comment` is a free string; the `missing / requirements / explanation / requires_attention` layout is a **convention**, configurable per user.
- Wiring: `scoring_prompt_file` XOR inline `scoring_prompt` in the pipeline YAML (same pattern for `apply_prompt_file`, `test_prompt_file`).
- Location convention: either within cloned repo `pipelines/prompts/scoring-<profile>.xml.j2` (create `pipelines/` if absent); one profile = one prompt file, pipeline alongside as `pipelines/pipeline.<profile>.yaml`. Or ask user if working with installed package without clone.

## Blocks

Assemble from these; mandatory blocks always present, conditional blocks depend on intake.

| Block | Presence | Source |
| --- | --- | --- |
| `<system_prompt>` + `<instructions>` wrapper + task sentence | always | fixed |
| Candidate lens: role, tiers of stack, domains, and **what candidate is NOT** | always | resume |
| Skill tiering + weights (key / secondary / negligible) | ask user — recommended | enables flexible ranking |
| Adjacency / inference rules for skills not mentioned explicitly (PG→SQL, React→React Router, Java→Gradle) | mandatory; **ask user for the actual rules** | real resumes omit skills due to size limits → prevents false under-scores |
| Alternative related skills ("Vue, но рассмотрим опыт с React") | ask user | market scan |
| Seniority / years-of-experience = secondary | ask user | intake |
| Leadership: yes / no / partially | ask user; map each answer to score guidance | intake |
| Custom rules | user-defined slot | e.g. "infra does not lower the score; DevOps-primary role = 2" is an example of one |
| Archetype → score table (real vacancy categories) | always | Stage II market scan |
| Hard filters (sub-middle, internships, people management, mandatory foreign skills/technologies) | always | intake only — never add unprompted |
| `<requires_attention>` tag rule | always | fixed convention |
| 1–5 scale | always | fixed skeleton |
| `<output_format>` JSON schema + `<comment_string_format>` | always | fixed; labels configurable |
| `{{ resume_formatted }}` / `{{ vacancy_formatted }}` injection | always | fixed |
| `<examples>` | always — **≥8 recommended, 2–3 per rules cluster** | real vacancies; more rules ⇒ more examples |
| `<anti_examples>` + `<why_incorrect>` | always — **1–3 default**; grow from observed real failures | real boundary/junk vacancies |
| `<final_task_summary>` | always | fixed |

Reference implementation to decompose: `scoring-java-fullstack.xml.j2` bundled with this skill.

## Stage I — Intake + candidate lens

**Precondition:** none. **Approval:** not required, proceed directly. **Output:** profile fact sheet.

1. Read the resume. Extract role profile, core/secondary/ignored stack, domains, seniority, and what the candidate does *not* do.
2. Ask (never skip):
   - Target roles and **anti-roles** (what must score low).
   - Stack tiers and which skills are key vs negligible.
   - **Adjacency/inference rules** the user accepts (list candidates derived from the resume; user confirms).
   - Tolerance for **related** (not matching) technologies.
   - Seniority floor and how to treat years.
   - Leadership: yes / no / partially — and the wanted score mapping.
   - Mandatory **foreign skills/technologies to hard-exclude**.
   - Is `<requires_attention>` relevant?
   - Comment language; strictness dial (avoid false positives vs false negatives); intended `min_required_score`.
3. Present the fact sheet back for correction before proceeding.

## Stage II — Market scan + archetype mapping

**Precondition:** I done. **Approval:** required. **Output:** archetype→score table + candidate example pool. **Interactive.**

1. Build/reuse searches with the `jobfucker-collecting-hh-vacancies` skill; preview with `jobfucker search --pipeline-id <id> --query '<q>' --params '<json>' --format json`.
2. `search` is listing-only: it returns titles/snippets, **not full descriptions**. Use it for real archetypes, titles, counts, and junk detection. Full-description examples need Stage IV (fetch) or a user-pasted vacancy.
3. Show real results to the user; ask **what is a good fit and what is not**. This drives the archetype→score table and the example/anti-example pools.
4. Record each archetype with the count, and the real titles that back it. No invented archetypes.
5. Draft/refresh hard filters from the disliked results — only those matching user-stated exclusions.

## Stage III — Prompt assembly + wiring

**Precondition:** I (II recommended for the archetype table). **Approval:** required. **Output:** live prompt.

1. See `scoring-java-fullstack.xml.j2` example bundled with this skill. It is not example to copy but more like structure to follow.
2. Select blocks from the table; write `pipelines/prompts/scoring-<profile>.xml.j2`.
3. Keep measured counts, traps, and rationale as Jinja comments next to the relevant block.
4. Upd the pipeline YAML: `scoring_prompt_file` (or inline).
5. New pipeline: `jobfucker init --config <file>`. Existing: `jobfucker update --config <file>`.
6. Verify: YAML parses; prompt renders; only the two documented variables used; JSON schema block unchanged.

## Stage IV — Real-score calibration

**Precondition:** III. **Approval:** required for **fetch** (network, slow). **Output:** calibrated rules.

1. With approval: `jobfucker fetch --pipeline-id <id> --take N` → `jobfucker score --pipeline-id <id>`. N should be ~10-20 to avoid long feedback loop when experimenting. Do full fetch only when final prompt is ready.
2. `jobfucker vacancies dump [--pipeline-id <id>]` — inspect `editable.score` and `editable.score_reasoning` per vacancy.
3. Compare against expectation; classify each disagreement (over-scored junk / under-scored gold).
4. Map each disagreement to its owning block; patch only that block
5. Re-run `score` and re-dump to confirm the shift.

## Stage V — Example growth

**Precondition:** any dump (IV recommended). **Approval:** not required. **Output:** expanded `<examples>` / `<anti_examples>`.

- Promote real, correctly-scored vacancies into `<examples>` (keep `job_context` compressed).
- Promote real failures and false positives into `<anti_examples>` with a non-empty `<why_incorrect>`.
- Keep counts documented; ≥8 positives recommended, 1–3+ anti-examples, both growing with real evidence only.

## Stage VI — Recalibration loop

**Precondition:** a live prompt, real scores exist. **Approval:** required. **Output:** minimal prompt patch + changelog explanation.

Once basic prompt is ready: `vacancies dump` → classify disagreements → patch the owning block only → explain the change to the user.

## Gates — done only when

- Prompt parses and renders with exactly `resume_formatted` + `vacancy_formatted`.
- Output schema untouched (`fit_score` 1..5 + `comment`).
- Every archetype backed by a vacancy actually seen.
- Hard filters reflect user-stated exclusions only.
- Examples agree with the 1–5 scale; every anti-example has `<why_incorrect>`.
- Pipeline stored via `init`/`update`; no fetch without approval.

## Anti-patterns

- Instruction dump with no intake; contradictory rules.
- Invented archetypes or imagined vacancy behavior.
- Unprompted hard filters; schema edits.
- Iterating without a dump snapshot.
- Running a stage without explicit approval.

## Where to find commands

CLI syntax and flag windows change. Read `jobfucker search --help`, `... fetch --help`, `... vacancies dump --help`, and the project `AGENTS.md`. Search design lives in `jobfucker-collecting-hh-vacancies`.
