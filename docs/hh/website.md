# Website Behavior and DOM

Use semantic URL/state checks first and `data-qa` selectors second. CSS module class names are
generated and are not a stable automation contract. HH can render different login surfaces at
similar-looking URLs, so detect the active surface from DOM markers.

## Login surfaces

### Direct new-SPA login

Typical entry: `https://hh.ru/login`. Recognition marker: applicant/employer role cards.

Flow and stable selectors:

| Step | `data-qa` selector/shape |
| --- | --- |
| Applicant role | `[data-qa^="account-type-card-APPLICANT"]` |
| Employer role | `[data-qa^="account-type-card-EMPLOYER"]` |
| Role-card container | `account-type-cards` |
| Continue/submit | `submit-button` |
| Credential switch | `credential-type-switch` |
| Email tab | `[data-qa^="credential-type-email"]` |
| Phone tab | `[data-qa^="credential-type-phone"]` |
| Email input | `applicant-login-input-email` |
| Phone input wrapper | `applicant-login-input-phone` |
| Phone calling code | `magritte-phone-input-calling-code` |
| Phone national number input | `magritte-phone-input-national-number-input` |
| Expand password login | `expand-login-by-password` |
| Password input | `applicant-login-input-password` |
| Password visibility | `password-visibility-switch-button` |
| Error | `form-helper-error` |

Selected controls can carry a literal suffix in the attribute value, for example
`data-qa="account-type-card-APPLICANT checked"`. Prefix matching is therefore safer than exact
matching for role and credential tabs.

Controls appear progressively: select the applicant role, continue, choose email/phone, fill the
identifier, expand password mode, then fill and submit the password. The page has a hidden `_xsrf`
input.

### Legacy OAuth-context login

Reached after an unauthenticated request to the real OAuth authorize URL redirects to an account
login URL. Recognition marker: `login-input-username` with password/code buttons and no role cards.

| Element | Selector |
| --- | --- |
| Form | `[data-qa="account-login-form"]` |
| Username | `input[data-qa="login-input-username"]` |
| Password-mode button | `button[data-qa="account-login-submit-by-password"]` |
| Code-mode button | `button[data-qa="account-login-submit-by-code"]` |
| Password | `input[data-qa="login-input-password"]` |
| One-time-code container | `div[data-qa="account-login-code-input"]` |
| PIN input | `input[data-qa="magritte-pincode-input-field"]` |
| Final submit | `[data-qa="account-login-submit"]` |
| Error | `[data-qa="form-helper-error"]` |

Both password and one-time-code flows can lead to a usable OAuth session. Any PIN/SMS/email code is
a human interaction, not a value to infer or retry automatically.

### Recognition rule

```text
account-type-card-* present   → new-SPA flow
login-input-username present  → legacy OAuth-context flow
neither present               → already authorized, challenge, error, or changed page
```

Do not decide from `/account/login` versus `/login` alone. Verify the markers on every run.

For browserless login and OAuth details, see [Authentication](authentication.md).

## Search results page

Route: `https://hh.ru/search/vacancy?...`. Initial results are server-rendered; no
`api.hh.ru` XHR is required to populate the page.

Useful structural selectors:

| Purpose | Selector |
| --- | --- |
| Results container | `[data-qa="vacancy-serp__results"]` |
| Vacancy card | `[data-qa^="vacancy-serp__vacancy"]` |
| Title link | `[data-qa="serp-item__title"]` or `serp-item__title-text` |
| Employer | `[data-qa="vacancy-serp__vacancy-employer"]` |
| Address | `[data-qa="vacancy-serp__vacancy-address"]` |
| Compensation | `[data-qa="vacancy-serp__compensation"]` |
| Apply control | `[data-qa="vacancy-serp__vacancy_response"]` |
| Results heading | `[data-qa="vacancies-search-header"]` |
| Search input | `[data-qa="search-input"]` |
| Pagination | `[data-qa="pager-block"]`, `pager-page`, `pager-next` |
| More filters | `[data-qa="header-search-more-filters"]` |
| Save search | `[data-qa="vacancy-saved-search-create"]` |
| Compact result display | `input[data-qa="short_description"]` |
| Expanded result display | `input[data-qa="full_description"]` |

Filter chips use names such as:

```text
search-filter-area-chip
search-filter-metro-chip
search-filter-district-chip
search-filter-experience-chip
search-filter-employment_form-chip
search-filter-work_schedule_by_days-chip
search-filter-working_hours-chip
```

Vacancy title links contain `/vacancy/<id>`, making the ID recoverable without depending on a
generated class. Apply links commonly point to `/applicant/vacancy_response?vacancyId=<id>…`.
`short_description` and `full_description` are radio controls for result-card display density;
they are not vacancy-description content nodes.

### Catalog versus resume-scoped page

| URL discriminator | Meaning | Visible marker |
| --- | --- | --- |
| `from=header-menu` and no active resume scope | Catalog search | `Найдено N вакансий` |
| `resume=<owned-id>&from=resumelist` | Resume similarity search | `Найдено N подходящих вакансий для резюме` |

The second form requires an authenticated owner session. When logged out, the resume scope can be
silently ignored and the whole catalog rendered. Verify the heading and result scale, not just the
URL. API clients should use the explicit operations in [Vacancy search](api/search.md#search-modes).

## Vacancy page and response UI

- Vacancy route: `/vacancy/{id}`.
- The API's `alternate_url` points to this web page.
- Search cards can expose a response link at
  `/applicant/vacancy_response?vacancyId={id}&employerId={id}…`.
- A vacancy with `response_url` may send the applicant to an external employer flow; the API client
  should skip it unless external applications are explicitly supported.
- A vacancy with a required test uses the website response/test flow rather than plain
  `POST /negotiations`; the test submission contract is not established enough for production use.

For API preflight and authoritative application results, see
[Applications and negotiations](applications-and-negotiations.md).

## CAPTCHA DOM

The standalone HH text challenge exposes:

| Element | Selector/name |
| --- | --- |
| Image | `img[data-qa="account-captcha-picture"]` |
| Refresh | `[data-qa="captcha-renew-text"]` |
| Language | `[data-qa="captcha-language"]` |
| Text input | `input[data-qa="account-captcha-input"]`, name `captchaText` |
| Submit | `[data-qa="account-captcha-submit"]` |
| Wrong-answer message | `[data-qa="account-captcha-error"]` |
| XSRF | hidden input `_xsrf` |

The login-flow variant renders the same picture/input controls inside a modal
(`data-qa="modal-overlay"`, heading «Пройдите капчу») and validates the answer via a re-submit of
the login form, not via `account-captcha-submit`. See
[Login CAPTCHA](captcha-login.md).

The server-rendered image `src` can initially be empty; the frontend obtains the actual image key
through `POST /captcha?lang=RU`. Do not scrape a key from the HTML. Full protocol:
[CAPTCHA](captcha.md).

## Web-only XHR headers

State-changing applicant website calls can require:

```http
X-Xsrftoken: <_xsrf cookie>
X-Requested-With: XMLHttpRequest
X-Hhtmfrom: <page context>
X-Hhtmsource: <operation context>
Referer: https://hh.ru/<relevant-page>
```

These headers do not belong on every API request. Keep website and applicant-API request builders
separate even if they share a cookie jar.
