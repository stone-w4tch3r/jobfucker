# HH.ru Integration Wiki

Canonical technical reference for building and maintaining the HH.ru client.

The wiki describes HH.ru as it behaves, not how it was researched. It combines the public JSON
API, the server-rendered website, OAuth, browserless login, CAPTCHA, and applicant workflows into
one cross-linked model.

> Freshness: behavior was validated against live `hh.ru` and `api.hh.ru` on 2026-08-17 and
> 2026-08-29. HH.ru is
> an external system and can change without notice. Revalidate the canaries in
> [Client blueprint](client-blueprint.md#change-canaries) when behavior drifts.

## Start here

| Need | Read |
| --- | --- |
| Understand which HH surface owns which operation | [Platform map](platform-map.md) |
| Log in and obtain or refresh a Bearer token | [Authentication and OAuth](authentication.md) |
| Build the HTTP layer and classify failures | [Transport and errors](api/transport-and-errors.md) |
| Find an endpoint and its auth/body requirements | [Endpoint catalog](api/endpoints.md) |
| Implement vacancy search and filters | [Vacancy search](api/search.md) |
| Decode API payloads | [Response models](api/response-models.md) |
| Automate or inspect the website | [Website behavior and DOM](website.md) |
| Detect and solve a challenge | [CAPTCHA](captcha.md) |
| Solve the CAPTCHA inside the login flow | [Login CAPTCHA](captcha-login.md) |
| Apply and read negotiations/messages | [Applications and negotiations](applications-and-negotiations.md) |
| Take employer screening tests at apply | [Tests](tests.md) |
| Turn the behavior into a resilient client | [Client blueprint](client-blueprint.md) |
| See what is not yet established | [Known unknowns](known-unknowns.md) |

## Core model

```text
credentials
    │
    ▼
hh.ru login ── session cookie (`hhtoken`) ──► hh.ru OAuth authorize
                                                    │ 302 code
                                                    ▼
                                             hh.ru OAuth token
                                                    │ Bearer
                                                    ▼
                                    api.hh.ru applicant operations
                                      search / detail / apply / chat

Any request ── challenge signal ──► hh.ru/account/captcha
                                    key → PNG → answer → retry
```

The nominal client workflow is browserless: credential login without a challenge, OAuth code
capture, token exchange, API operations, standalone HH text CAPTCHA solving, and the embedded
login CAPTCHA (solved by re-submitting the login form with the captcha fields) have verified
pure-HTTP paths. A browser remains a recovery interface for challenges the solver cannot pass and
for web-only operations.

## Non-negotiable facts

- Core API reads require a Bearer token. Anonymous `/vacancies`, vacancy detail, `/me`, resumes,
  employers, and negotiations return `403 forbidden`.
- The website and API are different transports. Search pages are server-rendered and do not call
  `api.hh.ru` from the browser.
- There are two distinct vacancy search modes: catalog search and resume-scoped similarity search.
  `resume` and `resume_id` on `/vacancies` are silently ignored.
- HTTP status alone does not determine success. Search parameter errors can be returned as HTTP
  200 bodies containing `errors` and no `items`/`found`.
- Apply success is HTTP `201` with an empty body. A `403 already_applied` response is authoritative;
  vacancy `relations` is not a reliable pre-check.
- A `403 captcha_required` is recoverable: solve the standalone challenge, then retry the original
  Bearer request. The unlock is account/Bearer-bound; API cookie synchronization is unnecessary.
- Treat all external JSON as additive. Decode required fields, tolerate absent optional fields,
  and ignore unknown keys.

## Scope and authority

This wiki is authoritative for observed HH behavior and the integration constraints derived from
it. Product behavior and board-neutral interfaces remain authoritative in the parent jobfucker
specifications. The implementation requirements that turn these facts into `jobfucker.clients.hh`
live in the [HH.ru client specification](../specs/hh-client.md).

Raw request captures, cookies, tokens, screenshots, and exploratory notes are deliberately not
dependencies. The wiki is self-contained and contains the durable, sanitized contracts.
