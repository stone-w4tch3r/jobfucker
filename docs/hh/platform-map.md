# Platform Map

HH.ru exposes three related but non-interchangeable HTTP surfaces.

| Surface | Base | Authentication | Typical content | Use |
| --- | --- | --- | --- | --- |
| Applicant API | `https://api.hh.ru/` | `Authorization: Bearer …` | JSON | Search, vacancy detail, resumes, applications, negotiations, messages |
| OAuth | `https://hh.ru/oauth/` | Session cookie for authorize; form credentials for token | Redirects and JSON | Turn an authenticated web session into access/refresh tokens |
| Website | `https://hh.ru/` | Cookies + XSRF for state-changing web calls | Server-rendered HTML and SPA JSON | Login, CAPTCHA, search UI, test/trash and other web-only workflows |

## Authentication boundaries

- A website session cookie does not authenticate `api.hh.ru`; API calls require a Bearer token.
- A Bearer token does not replace cookies/XSRF on website-only XHR endpoints.
- `hhtoken` is the OAuth-decisive website cookie. `hhrole` and `hhul` mainly affect what the web UI
  renders and are not proof that OAuth authorize will succeed.
- The strongest liveness check is `GET https://api.hh.ru/me` with the Bearer token. For a session
  that has not minted a token yet, probe the OAuth authorize response: a code redirect proves the
  session is OAuth-capable.

See [Authentication and OAuth](authentication.md) for the full chain.

## Anonymous behavior

Core applicant/catalog resources are protected:

| Request without Bearer | Result |
| --- | --- |
| `/vacancies`, `/vacancies/{id}`, `/employers/{id}` | `403` API error envelope |
| `/me`, `/resumes/mine`, `/negotiations` | `403` API error envelope |
| Bogus vacancy ID | `403` before resource lookup; authenticated lookup reaches `404` |

Public directory/suggest resources are exceptions:

- `GET /industries` → `200`, bare JSON array.
- `GET /suggests/areas`, `/suggests/area_leaves`, `/suggests/professional_roles`,
  `/suggests/companies`, and `/suggests/vacancy_search_keyword` → `200`, normally `{items: [...]}`.
- `/suggests/metro` returned `404` anonymously; obtain metro station IDs from vacancy-detail
  `address.metro_stations[].station_id` unless a supported lookup is found.

Anonymous website search does work. `https://hh.ru/search/vacancy?...` is server-rendered, but its
HTML contract is different from the JSON API and should not be a transparent API fallback.

## Operation routing

| Operation | Preferred surface | Browser needed? | Notes |
| --- | --- | --- | --- |
| Credential login | Website HTTP | Normally no | Multipart `/account/login`; an embedded login CAPTCHA is solvable by re-submitting the login form with captcha fields ([Login CAPTCHA](captcha-login.md)) |
| OAuth code capture | OAuth HTTP | No | Authenticated authorize returns 302 to `hhandroid://oauthresponse?code=…` |
| Token exchange/refresh | OAuth HTTP | No | OAuth error envelope differs from API errors |
| Search/detail/resumes | Applicant API | No | Bearer-only |
| Apply | Applicant API | No | Bearer + form body; no XSRF |
| Negotiations/messages | Applicant API | No for verified reads | Message send is documented but not live-verified here |
| Standalone API-triggered CAPTCHA | Website HTTP + retry API | No | Verified key → PNG → answer → submit protocol |
| Embedded login CAPTCHA | Login form HTTP | No | Re-submit login form with `captchaKey`/`captchaText`/`captchaState`; picture/answer protocol shares the standalone key/image mechanics ([Login CAPTCHA](captcha-login.md)) |
| Vacancy tests | Website/XSRF | Possibly | Contract known only partially; not verified end-to-end |
| Hide/delete chat | Website/XSRF | Possibly | Not live-verified |
| Resume create/edit | API/web | Unknown | Deferred; do not infer from list/read behavior |

## Search page versus search API

Both website search entry points use `/search/vacancy`, but the server chooses a different search
pipeline from the URL parameters:

| Website URL shape | UI marker | Equivalent API operation |
| --- | --- | --- |
| `?area=1&from=header-menu…` | `Найдено N вакансий` | `GET /vacancies?area=1` |
| `?resume=<id>&from=resumelist` | `Найдено N подходящих вакансий для резюме` | `GET /resumes/{id}/similar_vacancies` |

`from=resumelist` activates resume-scoping on the website. A `resume` parameter without it is
ignored. On the API, only the dedicated `similar_vacancies` route provides resume-scoped search.
See [Vacancy search](api/search.md#search-modes).

## Infrastructure behavior

Both hosts sit behind DDoS-Guard. A request may fail before HTTP with a TLS EOF. No useful anonymous
rate-limit or retry headers were observed. A desktop Chrome-like user agent plus bounded retry is
more reliable than an Android-looking user agent from a generic HTTP stack. Preserve TLS
verification and distinguish connection failure from API rejection.

The transport rules are canonical in [Transport and errors](api/transport-and-errors.md).
