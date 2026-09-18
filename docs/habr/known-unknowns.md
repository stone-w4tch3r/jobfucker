# Known Unknowns

Integration boundaries that are not established well enough to implement as facts. Not a backlog or
research diary. A feature depending on one of these items requires focused verification first, then
the item moves to a canonical page and is removed from here.

## Authentication (Session 2)

- Full SSO redirect chain for `/users/auth/tmid`: exact hops, cookie name(s) and domains, whether
  `habr.com` and `career.habr.com` share the session, and any `tmid` token semantics.
- Session lifetime, refresh, and logout (`/users/sign_out` form) behavior.
- Whether API requests need only the session cookie or also a CSRF header, and which header name.
- Whether a browserless credential login exists at all, or whether SSO forces a browser/human step.
- Anonymous capability: what lists/details are reachable without a session, and whether an anonymous
  request faces a challenge.

## Search and listings (Session 2)

- The full query-parameter surface of `/vacancies` (text, skills, salary, grade, remote, city,
  experience, company, sorting).
- Native page size and whether `per_page` is honored on the HTML listing (RSS honors it).
- Listing cap / maximum accessible items.
- Whether `type=suitable` requires an owned resume and how it is selected.
- Whether any XHR/fragment endpoint exists for "load more" or listing refresh.
- RSS field completeness versus HTML cards, and whether RSS is a viable `list_vacancies` source.

## Vacancy detail (Session 2)

- JSON-LD field completeness (salary, location, employment type, skills, company) versus DOM fields.
- Archived / closed vacancy representation and the corresponding detail response.
- External-application ("отклик на другом сайте") representation.
- Screening-test or questionnaire presence flag, if any.

## Resumes (Session 2)

- Where an owned resume lives and how to list/read it (`/{alias}`, `/profile/specialization`, or a
  JSON route not yet found).
- Whether one account has one resume or many, and how to obtain the resume id used at apply.
- Whether `/api/frontend_v1/resumes` has a resume-search contract relevant to us (it returned a
  public specialist list).

## Apply and responses (Session 3)

- The logged-in apply request: endpoint, method, body, required CSRF field/header.
- Resume selection and cover-letter mechanics.
- Success, duplicate/already-applied, vacancy-unavailable, external-application, and limit signals.
- Limit tracking: existence, daily cap, reset boundary, account dependence.
- Reconciliation reads: `/responses` (applications) and `/conversations` (dialogs) shapes.
- Screening tests / questionnaires at apply, if any.

## Challenges and infrastructure

- Whether any CAPTCHA or anti-bot gate exists, on which endpoints, and its engine.
  If encountered, follow the [CAPTCHA rule](research-playbook.md#captcha-handling-rule).
- Rate-limit behavior, retry headers, and pacing implications.
- Error taxonomy: reconcile the two observed envelopes (`{"httpCode",...}` vs `{"error":...}`),
  HTTP codes, and board-specific error codes; map to `ClientError`.

## Resolving an unknown

1. Define the smallest claim and success/failure signals.
2. Use the `habr-exp` experiment account for mutations or challenge provocation.
3. Keep secrets and raw captures outside version control (`/tmp`), then sanitize.
4. Update the canonical wiki page and tests; remove the item from this page.
