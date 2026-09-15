# Response Models

These are resilient decoding contracts for HH payloads, not exact closed schemas. HH adds fields
frequently. Require only what the use case needs, model documented nullable/optional fields, and
ignore unknown keys while preserving raw payloads only in bounded diagnostics when necessary.

## Paginated envelope

Most list operations return:

```text
items: list[T]
found: int
page: int            # zero-based
pages: int
per_page: int
```

Search adds optional `clusters`, `arguments`, `fixes`, `suggests`, and `alternate_url`. Correction
objects vary by search mode: catalog search has produced `suggests: {value, found}` with
`fixes: null`, while resume-scoped search has produced `fixes: {original, fixed}` with
`suggests: null`. Model both fields as separate nullable shapes. Resumes, negotiations, and
messages use the five-field envelope without search facets.

## Current user

`GET /me` returns an applicant identity. Useful fields:

```text
id, email, phone
first_name, middle_name, last_name
auth_type                 # observed "applicant"
is_applicant, is_employer, is_admin, is_in_search
resumes_url, negotiations_url, crypted_id, email_verified
counters:
  new_resume_views
  unread_negotiations
  resumes_count
```

Do not require a `new_negotiations` counter; it was absent from the observed payload.

## Resume list item

`GET /resumes/mine` is paginated. Important fields:

```text
id: str
title: str
url: str
alternate_url: str
status: {id: str, name: str}
created_at: datetime
updated_at: datetime
can_publish_or_update: bool | null
new_views: int
total_views: int
views_url: str
actions/download: optional objects
```

Items include many additional profile, visibility, employment, and role fields. There is no
required nested `counters` object. Only `status.id == "published"` resumes are usable for normal
application selection.

## Public directory and suggestion responses

Useful anonymous response shapes:

```text
GET /suggests/areas
  {items: [{id: str, text: str, url: str, parent: {id, text, url} | null}]}

GET /suggests/companies
  {items: [{id: str, text: str, url: str, area?, logo_urls?, industries: [{id, name}]}]}

GET /industries
  [{id: str, name: str, industries: [{id: str, name: str}]}]

GET /suggests/vacancy_search_keyword?text=...
  {items: [{text: str}], suggest_id: str}
```

The captured keyword-suggestion items did not contain result counts. Keep these DTOs additive and
nullable where optional fields vary by suggestion type.

## Vacancy search item

Key fields from `/vacancies` and `similar_vacancies`:

```text
id: str
name: str
url: str                       # API URL
alternate_url: str             # website URL
published_at: str              # ISO datetime with offset; verified in real listings (2026-08-31)
employer: {id, name, alternate_url, logo_urls?} | null
snippet: {requirement?, responsibility?} | null
salary: {from?, to?, currency, gross} | null
salary_range: object | null     # additive/newer alternative detail
area, address
schedule, experience, employment, employment_form, work_format
professional_roles: list
archived: bool
closed_for_applicants: bool
has_test: bool
response_url: str | null
adv_response_url: str | null
apply_alternate_url: str | null
relations: list
response_letter_required: bool
```

The `snippet` fields can contain highlight markup and are not the full description. `address` is
often null in search results. `employer` can be null for concealed employers.

Observed additive fields include `accept_temporary`, `working_days`, `working_hours`,
`work_schedule_by_days`, `working_time_intervals`, `working_time_modes`, `vacancy_properties`,
`allow_chat_with_manager`, `extra_labels`, and advertising/context metadata.

`relations` must not be used as the authoritative “already applied” check. See
[Applications](../applications-and-negotiations.md#already-applied).

## Vacancy detail

`GET /vacancies/{id}` includes most search-item fields plus:

```text
description: str                      # full HTML
key_skills: list[{name: str}]
branded_description: str | null       # separate employer-branded HTML
contacts: object | null
negotiations_url: str | null
suitable_resumes_url: str | null
test: {required: bool, ...} | null
employer: expanded object | null
initial_created_at, created_at, published_at
languages, driver_license_types
allow_messages, quick_responses_allowed
```

The full response is additive and contains presentation/branding fields. Keep `key_skills` as a
list. Parse salary structurally. For scoring text, convert `description` with an HTML parser that:

- decodes character references;
- inserts boundaries for `p`, `div`, `li`, headings, `br`, table rows, `dt`, and `dd`;
- collapses redundant whitespace without gluing adjacent list items;
- preserves Cyrillic and other Unicode text.

Do not strip tags with one regex, and do not substitute `branded_description` for the canonical
description unless the product explicitly chooses that behavior.

## Negotiation

`GET /negotiations` returns paginated items with this useful live shape:

```text
id: str
state: {id: str, name: str}
created_at, updated_at
resume: object
vacancy: search-item-like object
viewed_by_opponent: bool
has_updates: bool
has_new_messages: bool
messages_url: str
url: str
counters: {messages: int, unread_messages: int}
chat_states, source, chat_id, messaging_status
decline_allowed: bool
read: bool
applicant_question_state, tags, hidden
```

Do not require stale fields such as `messages_count`, `has_messages`, top-level `employer`, or
`alternate_url`.

Known state IDs include:

| ID | Meaning |
| --- | --- |
| `discard` | Declined/discarded |
| `interview` | Interview stage |
| `response` | Application/response state |
| `invitation` | Invitation |
| `hired` | Hired |

Treat unknown states as forward-compatible values and retain the raw ID.

## Message

`GET /negotiations/{id}/messages` returns paginated items:

```text
id: str
created_at: datetime
text: str
state: object/value
address: object | null
author: {participant_type: str, ...}
editable: bool
viewed_by_me: bool
viewed_by_opponent: bool
read: bool
```

Use `author.participant_type` for sender classification. Do not require `view_state` or `source`.

## Application response

`POST /negotiations` success has no JSON model:

```text
HTTP 201 Created
Content-Length/body: empty
```

Decode this as a unit success. Failure payloads use the API envelope described in
[Transport and errors](transport-and-errors.md#api-error-envelope).
