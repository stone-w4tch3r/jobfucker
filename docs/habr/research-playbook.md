# Research Playbook

Method every research session follows. Read this and [the research plan](research-plan.md) before
starting; update this file only when the method itself changes.

## Goal

Build a canonical, sanitized reference for observed Habr Career behavior, enough to implement a
board client against the [client contract](../specs/client-contract.md). Thin docs first, deepened
one topic per session. Not a one-shot map.

## Target: contract coverage

Every session updates the operation-routing table in [platform-map.md](platform-map.md). A topic is
"researched" when the relevant method has a verified surface and contract:

```text
authorize · search_vacancies · list_vacancies · get_resumes · get_identity · apply_to_vacancy
service_info.per_auth_daily_cap · service_info.max_search_items
```

## Browser session

Use the persistent experiment scope:

```bash
agent-browser --session habr-exp --restore habr-exp open <url>
```

- The `habr-exp` scope is an experiment Habr account; mutations and mild challenge provocation are
  allowed on it.
- Pass the same launch flags on every command within a task. Changing a launch-affecting flag
  relaunches the daemon and destroys tabs; never call bare `open`.
- Never close the persistent scope.
- Prefer `snapshot -i`, `eval --stdin`, and `network requests` over raw HTML dumps to keep context
  small.

For jobfucker's own browser work: patchright headless is sufficient **if the user-agent is a normal
desktop Chrome string**. The `HeadlessChrome` UA makes SmartCaptcha escalate to an image challenge;
overriding the UA passes (see
[Engine comparison](captcha.md#engine-comparison-patchright-vs-cloakbrowser)). CloakBrowser is an
optional robustness upgrade, not a requirement for this challenge.

## Recording traffic

```bash
agent-browser --session habr-exp --restore habr-exp network requests --clear
agent-browser --session habr-exp --restore habr-exp network har start /tmp/habr-<topic>.har
# ...drive the flow...
agent-browser --session habr-exp --restore habr-exp network har stop /tmp/habr-<topic>.har
agent-browser --session habr-exp --restore habr-exp network requests --type xhr,fetch
agent-browser --session habr-exp --restore habr-exp network request <id>
```

Authenticate before recording so credentials and SSO redirects are not captured. HARs are secrets:
keep them in `/tmp`, never in the repo, delete when done.

## Verification labels

Use exactly these in every table:

- `Verified` — observed end-to-end against the live board.
- `Known-unverified` — the surface was located but its contract was not exercised.
- `Unknown` — not established.

Each page carries a freshness date. Record small `Change canaries` per page and revalidate when
behavior drifts.

## Sanitization

Write durable contracts, not raw captures. Before committing to the wiki, strip cookies, tokens,
CSRF values, emails, account identifiers, and full sensitive bodies. Replace volatile values with
a placeholder and keep only the shape.

## Rules

- One fact, one home: link to a home doc, never duplicate.
- Never infer mutation payloads from read shapes.
- Do not treat a logged-in observation as anonymous behavior, or vice versa.
- Prefer structured sources over scraping: `ld+json`, JSON endpoints, RSS, then DOM selectors.
- Do not add a dated session log to the wiki.

## CAPTCHA handling rule

If a challenge appears on any endpoint:

1. **Stop the current task.** Do not guess, brute-force, or replay.
2. **Capture the challenge shape** — network requests the CAPTCHA itself makes (URLs, params,
   headers, cookies, responses), DOM/selectors/iframe, page HTML, the engine (Yandex SmartCaptcha /
   reCAPTCHA / hCaptcha / Habr-native / Cloudflare-Qrator), and the exact context that triggered it
   (endpoint, preceding request, pacing). Record to `/tmp`.
3. Write sanitized findings to `docs/habr/captcha.md`; add open questions to `known-unknowns.md`.
4. **Open the challenge in a headed browser and notify the user** through pair-interaction
   (dashboard `http://localhost:4848`) so they can see it and solve it.
5. **Observe the solve** — widget interaction, token/field created, submit requests, verification
   request, retry of the original request, resulting cookies/session delta.
6. Document the full **trigger → solve → resume** flow with canaries. Only then may later agents
   automate; until reliable trigger + completion docs exist, CAPTCHA is human-in-the-loop.
7. Never commit challenge captures, cookies, or tokens.

Current state for Habr Career: the login SmartCaptcha was solved first by a human and then
reproduced automatically by the stealth headless browser (2/2, checkbox only) — see
[CAPTCHA](captcha.md#automation-feasibility-stealth-headless). Policy:

- Repeat the documented automated click only at low login frequency on an owned account.
- Treat a missing/`non-ok` token, an image/advanced challenge, or a Qrator interstitial as
  escalation: stop automating and hand off to a human via the headed-browser protocol above.
- The challenge is risk-based; successes are not a guarantee. Do not build retry loops that hammer
  the challenge.

## Handoff

At the end of a session:

1. Update the affected canonical pages and the operation-routing table.
2. Move resolved items out of `known-unknowns.md`; add newly discovered ones.
3. Note in [research-plan.md](research-plan.md) if the remaining session plan changed.
