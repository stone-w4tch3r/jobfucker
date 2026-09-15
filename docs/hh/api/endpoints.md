# Endpoint Catalog

Base URLs:

```text
Applicant API  https://api.hh.ru/
OAuth          https://hh.ru/oauth/
Website        https://hh.ru/
```

Paths below are relative to the stated base. Unless marked public, applicant API endpoints require
a Bearer token.

## Identity and resumes

| Method | Path | Contract | Verification |
| --- | --- | --- | --- |
| `GET` | `/me` | Current applicant identity | Verified |
| `GET` | `/resumes/mine` | Paginated resumes owned by the account | Verified |
| `GET` | `/resumes/{id}` | Full resume detail | Known, not exercised in the final live matrix |
| `POST` | `/resumes` | Create resume, JSON body | Unverified |
| `POST` | `/resume_profile` | Clone resume, JSON body | Unverified |
| `POST` | `/resumes/{id}/publish` | Publish/update resume | Unverified |
| `GET` | `/resumes/{id}/similar_vacancies` | Resume-seeded vacancy search | Verified |

Only resumes with `status.id == "published"` should be offered for application. Resume creation,
editing, cloning, and publishing are outside the established contract; see
[Known unknowns](../known-unknowns.md).

## Vacancies and directories

| Method | Path | Contract | Verification |
| --- | --- | --- | --- |
| `GET` | `/vacancies` | Catalog/query vacancy search | Verified |
| `GET` | `/vacancies/{id}` | Full vacancy detail | Verified |
| `PUT` | `/vacancies/blacklisted/{id}` | Blacklist vacancy | Known, unverified |
| `GET` | `/employers/{id}` | Employer profile | Auth requirement verified; success shape not central here |
| `GET` | `/employers/blacklisted` | Paginated employer blacklist | Known, unverified |
| `PUT` | `/employers/blacklisted/{id}` | Blacklist employer | Known, unverified |
| `GET` | `/industries` | Bare industry array | Verified public |
| `GET` | `/suggests/areas` | Area autocomplete | Verified public |
| `GET` | `/suggests/area_leaves` | Area-leaf autocomplete | Verified public |
| `GET` | `/suggests/professional_roles` | Professional-role autocomplete | Verified public |
| `GET` | `/suggests/companies` | Company autocomplete | Verified public |
| `GET` | `/suggests/vacancy_search_keyword?text=…` | Search text autocomplete; `{items:[{text}], suggest_id}`; input 2–3000 chars | Verified public |
| `GET` | `/suggests/metro` | Metro autocomplete | Returned `404` anonymously; do not depend on it |
| `GET` | `/saved_searches/vacancies` | `{found, items}` for account-owned saved searches | Verified |
| `POST` | `/saved_searches/vacancies` | Create a saved search | Unverified mutation |

Search semantics and every verified query parameter are in [Vacancy search](search.md).

## Applications and negotiations

| Method | Path | Encoding/purpose | Verification |
| --- | --- | --- | --- |
| `POST` | `/negotiations` | Form: `resume_id`, `vacancy_id`, optional `message` | Verified |
| `GET` | `/negotiations?status=active&page=…&per_page=…` | Paginated negotiations | Verified read |
| `GET` | `/negotiations/{id}/messages` | Paginated message history | Verified read |
| `POST` | `/negotiations/{id}/messages` | Form: `message` | Known, not live-verified |
| `DELETE` | `/negotiations/active/{id}` | Decline/cancel, optional `with_decline_message` | Known, not live-verified |
| `PUT` | `/employers/blacklisted/{id}` | Blacklist employer | Known, not live-verified |

`GET /negotiations` does not accept `order_by`; sending it produces a bad-argument failure. See
[Applications and negotiations](../applications-and-negotiations.md).

## OAuth

| Method | URL | Purpose |
| --- | --- | --- |
| `GET` | `https://hh.ru/oauth/authorize` | Obtain authorization code through a 302 custom-scheme redirect |
| `POST` | `https://hh.ru/oauth/token` | Exchange a code or refresh an expired token |
| `DELETE` | `https://api.hh.ru/oauth/token` | Revoke the current Bearer token |

Exact query/form fields are in [Authentication and OAuth](../authentication.md).

## Website routes

| Method | Route | Purpose | Auth |
| --- | --- | --- | --- |
| `GET` | `/login` | Direct new-SPA login | Cookies/XSRF established here |
| `POST` | `/account/login?backurl=%2F` | Credential login | Multipart + `_xsrf` |
| `GET` | `/account/login?...oauth=true` | Login shell; exact surface depends on how it was reached | Session |
| `GET` | `/account/captcha?state=…` | Standalone CAPTCHA page | Challenge state + cookies |
| `POST` | `/captcha?lang=RU` | Issue CAPTCHA image key | `X-Xsrftoken` |
| `GET` | `/captcha/picture?key=…` | CAPTCHA PNG | Cookie jar |
| `POST` | `/account/captcha?...` | Submit CAPTCHA answer in query string | XSRF/XHR headers |
| `GET` | `/search/vacancy?...` | Server-rendered vacancy search | Public; resume-scoped form requires owner session |
| `GET` | `/vacancy/{id}` | Server-rendered vacancy page | Public/conditional |
| `GET` | `/applicant/vacancy_response?...` | Vacancy response/test shell | Applicant session |
| `POST` | `/applicant/vacancy_response/popup` | Submit response/test form | Applicant cookies + XSRF |
| `POST` | `/applicant/negotiations/trash` | Hide/delete chat | Applicant cookies + XSRF |

The last two website mutations are not part of the verified v1 path. Do not treat their documented
payloads as stable until revalidated. Website selectors and SSR behavior live in
[Website behavior and DOM](../website.md).
