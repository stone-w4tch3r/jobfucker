---
name: jobfucker-collecting-hh-vacancies
description: >-
  Convert resume or work search requirements into verified set of Headhunter search queries to populate local vacancy DB via jobfucker later.
  Use when building jobfucker vacancy bank, extending existing bank with fresh vacancies, backfilling older ones, or designing HH search pools (query sets, filters, counts, header checks).
  This skill is about search/preview only — not real fetch.
---

# Collecting HH Vacancies

Turn resume/requirements → verified HH search pool. Search-only. Fetch never. Bank lives in local DB.

## Input

- Resume file OR free-text requirements.
- Extract: role profile, core stack, secondary stack, seniority, foreign languages to hard-exclude.

## Ask user FIRST

1. **Width** — narrow/precise vs broad coverage. Show tradeoff in numbers (noise vs missed).
2. **Freshness** — vacancy age window (default 30; 60/90 for backfill) and new-only vs older too.
3. **Volume** — target bank size. Adjust via width + window.

Never skip. User owns these dials.

## Build query pool

- 5-10 searches. Each: query string + filter block + rationale.
- **Search field = width dial.** `fields_to_search_in: [name]` default (narrow). Wide option: `[name, description]` — title + body. Pick depending on width. Hybrid: name-only precision sets for the core + one description-search set to sweep generic titles. Title-only search: precise, verifiable, but **misses generic-titled vacancies** ("Разработчик", "Программист" — stack only in body). Generic titles are real and common. Wide = also search description bodies: catches them, but junk explodes (any ad mentioning the keyword), dedup grows, verification cost jumps.
- Keywords: role words, stack words, eng + translit + rus variants.
- **AND/OR grouping**: `(A OR B) (C OR D)` = stack × scope combos (e.g. `.NET` × `AI`). Group-AND works.
- Precision sets: core stack set + narrowed fullstack set + rare-niche take-all sets.
- Location sets, eg remote/hybrid + big-cities + relocation.

## HH mechanics (verified quirks — trust these)

- **Region quirk**: area filter ALWAYS includes REMOTE. Region set ⊇ remote set. Overlap expected, fetch dedups later.
- **Sorting**: `relevance` ≈ `publication_time` for OR-name queries (identical junk ratio). Use `publication_time`: deterministic, fresh-first.
- `exclude_words`: block junk unrelated to search profile, eg junior/intern/стажер when searching senior positions.
- Quoted phrases unreliable — hh splits/loosens them.

## Verification protocol (mandatory, catches junk)

1. Run search. **Count**: too high or too low = bad query.
2. **Header check top 20-40 titles**. hh ranking arbitrary — read real titles, not counts.
3. Repeat per candidate until clean. Iterate.

### Junk-traps (measured, all real)

Relevant for AI related job search

- `Agentic` → stems to `агент` → ~1100 real-estate roles. Dead term.
- `"AI-агент"`/hyphen terms → split to `AI` + `агент` (OR'd) → same real-estate spam.
- `нейросет` → 200+ "обучение нейросетей" junk (юрист/SMM/маркетолог). Dead term.
- bare `агент` → real-estate. Dead term.
- `copilot OR chatgpt OR gigachat OR yandexgpt` → 5 results, junk. Dead term.
- `dotnet`/`Blazor`/`".NET Core"` → stem to `.NET`, marginal +1. Skip.
- Multi-agent titles already caught via `LLM`/`RAG` terms — no separate agent search needed.

## Volume math (decide, then tell user)

- Narrowed combos often **strict subsets** of core sets → add 0 unique. Check before promising volume (e.g. fullstack∧.NET ⊆ .NET core: 27 ⊂ 234).
- Report real market size honestly. If target unreachable without noise, say so.
- Growth levers, ranked by noise cost: widen `published.within_days` (60/90) > broaden stack terms > search field name→name+description > drop exclude_words.
- Raw sum ≠ unique: region sets ⊇ remote sets, subsets collapse. Estimate unique after dedup.

## Incremental mode (extend existing bank)

- **New refresh**: re-run same searches on rolling window → surfaces fresh vacancies. Fetch idempotent (dedup by external_id). Re-run search = safe preview.
- **Backfill old**: pool exhausted → widen `within_days` on same queries (or older from/to range).
- Check search output DB status (new vs fetched/scored/applied) to see real deficit.

## Output artifact

- Save ALL verified sets as entries of `service.<board>.searches[]` in the pipeline YAML (convention: this repo's `pipelines/pipeline.*.yaml`). One profile = one file.
- Each entry is self-contained: its `query` + its own `filter` block (+ optional `window`).
- New pipeline: `uv run poe app init --config <file>`. Existing pipeline: edit pool, then `uv run poe app update --config <file>`.
- Keep measured counts, dates, traps, rationale as YAML comments next to entries.
- Validate YAML parses after edit; re-preview a set by pasting its query/filter into `jobfucker search --pipeline-id <id> --query '<q>' --params <file> --format json`.

## Quality gates — done only when

- Every set header-checked clean.
- Counts recorded with date.
- Traps absent.
- YAML validates and pipeline stored via `init`/`update`.
- No fetch performed (search/preview only, listing-level).

## Follow-up

- `jobfucker fetch --pipeline-id <id>` walks every `searches[]` entry into the DB. Only on explicit user behalf/approve.

## Where to find commands

CLI syntax, pipeline-id discovery, `search` flags change over time. Read them in the project's own AGENTS.md / `app search --help`.
