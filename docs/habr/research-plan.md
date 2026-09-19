# Habr Career Research Plan

Roadmap and method for researching `career.habr.com` and growing a Habr Career wiki that eventually
reaches parity with the [HH.ru integration wiki](../hh/README.md). This is the durable handoff for
every research session: read it before starting, update it only when the plan itself changes.

## Goal

Produce a canonical, sanitized reference for observed Habr Career behavior — enough to implement a
board client against the [client contract](../specs/client-contract.md) — by building thin docs
first and deepening them session by session. Not a one-shot full map.

## Target: Client contract coverage

Research is "done enough to build" when every generic member has a verified Habr surface:

```text
service / service_info (per_auth_daily_cap, max_search_items)
authorize
search_vacancies
list_vacancies
get_resumes
get_identity
apply_to_vacancy
aclose
```

That table is the spine of the wiki. Every session updates it.

## Deliverable shape (mirror `docs/hh/`)

```text
docs/habr/
  README.md              # wiki entry: core model, non-negotiables, scope/authority
  platform-map.md        # surfaces + operation routing + contract-coverage table
  known-unknowns.md      # open questions (not a research diary)
  research-playbook.md   # method every session follows
  authentication.md
  website.md
  captcha.md             # only if/when a challenge is observed
  applications-and-responses.md
  api/search.md
  api/response-models.md
  api/transport-and-errors.md
```

Final step adds `docs/specs/habr-client.md` and AGENTS.md rows. Raw HARs, cookies, tokens, and
challenge captures live in `/tmp`, never in the repo; docs carry sanitized durable contracts only.

## Sessions

| # | Session | Goal | Deliverables | Exit criteria |
| --- | --- | --- | --- | --- |
| 1 | World map + playbook | Surface discovery: HTML vs XHR vs `/v1` vs GraphQL vs RSS; framework; auth mechanism; candidate endpoint per Client method; seed unknowns; write method + CAPTCHA rules | `README.md`, `platform-map.md`, `known-unknowns.md`, `research-playbook.md`, this file | every Client method mapped to a candidate surface + status; playbook usable by later agents |
| 2 | Auth + all reads | SSO `tmid` flow, cookies/CSRF/session, any token or `/v1` auth, `whoami`; transport/errors/anti-bot; search listing params + paging; vacancy detail; resumes | `authentication.md`, `api/transport-and-errors.md`, `api/search.md`, `api/response-models.md` | `authorize`, `get_identity`, `search_vacancies`, `list_vacancies`, `get_resumes` verified; error→`ClientError` map; `resume_id` semantics |
| 3 | Apply + responses | Apply request (method/body/CSRF), resume + letter, success/duplicate/limit/external/test signals, reconciliation reads (`/responses`, `/conversations`), limit tracking | `applications-and-responses.md` | exhaustive `ApplyResult` mapping; `apply_to_vacancy` verified |
| 4 | Consolidate → spec | Promote wiki to `docs/specs/habr-client.md`, capability protocols, AGENTS.md rows, canaries | spec + AGENTS updates | implementation-ready spec |

Session 2 is the largest; split into reads-auth/search and detail/resumes only if it overflows.
Sessions may split, but the total should stay small (roughly 2–4 core sessions plus splits).

**Status (2026-09-19):** Sessions 1–3 complete. `apply_to_vacancy` is verified (apply / letter /
withdraw / `ApplyResult` map in [Applications and responses](applications-and-responses.md)).
`service_info.per_auth_daily_cap` is resolved as a **150 responses/month** account quota (not daily;
deletes still count) — only its reset boundary stays open. Session 4 (spec + AGENTS rows) is the
remaining step; residual unknowns that do not block implementation stay in
[known-unknowns.md](known-unknowns.md).

## CAPTCHA handling rule (all sessions)

If a challenge appears on any endpoint:

1. **Stop the current task.** Do not guess, brute-force, or replay.
2. **Capture the challenge shape** — network requests the CAPTCHA itself makes (URLs, params,
   headers, cookies, responses), DOM/selectors/iframe, page HTML, the engine (Yandex SmartCaptcha /
   reCAPTCHA / hCaptcha / Habr-native / Cloudflare-Qrator), and the exact context that triggered it
   (endpoint, preceding request, pacing).
3. Write sanitized findings to `docs/habr/captcha.md`; add open questions to `known-unknowns.md`.
4. **Open the challenge in a headed browser and notify the user** through pair-interaction
   (dashboard `http://localhost:4848`) so they can see it and solve it.
5. **Observe the solve** — widget interaction, token/field created, submit requests, verification
   request, retry of the original request, resulting cookies/session delta.
6. Document the full **trigger → solve → resume** flow with canaries. Only then may later agents
   automate; until reliable trigger + completion docs exist, CAPTCHA is human-in-the-loop.
7. Never commit challenge captures, cookies, or tokens — sanitize to `/tmp`.

## Research constants

- Browser: `agent-browser --session habr-exp --restore habr-exp open <url>`. The `habr-exp` scope
  is the experiment Habr account (mutations and mild challenge provocation allowed). Pass the same
  launch flags on every command within a task or the daemon relaunches and tabs die. Never close
  the persistent scope.
- HAR: authenticate before recording. `network har start` → drive the flow → `network har stop
  /tmp/habr-<topic>.har`; inspect with `agent-browser network requests` / `network request <id>`.
- Secrets: HARs, cookies, tokens, CSRF values, and challenge captures stay in `/tmp` and are deleted
  after sanitizing.
- Verification labels in every table: `Verified` / `Known, not exercised` / `Unknown`. Each doc
  carries a freshness date.
- One fact, one home: link to a home doc, never duplicate its facts.
- Never infer mutation payloads from read shapes.
- Record change canaries per doc (see HH's
  [client blueprint](../hh/client-blueprint.md#change-canaries)) and revalidate when behavior drifts.
