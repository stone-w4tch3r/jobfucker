# Applications and Negotiations

## Apply contract

```http
POST https://api.hh.ru/negotiations
Authorization: Bearer <token>
Content-Type: application/x-www-form-urlencoded

resume_id=<published resume owned by this token>
vacancy_id=<vacancy id>
message=<optional cover letter>
```

Wait a randomized 1–3 seconds before the POST. Website cookies and XSRF are not required for this
API operation.

Success is exact and unusual:

```text
HTTP 201 Created + empty body → application submitted
```

Do not require `{}` JSON and do not treat every 2xx response as this success without checking the
endpoint contract.

## Preflight

Fetch vacancy detail before applying and make deterministic skip decisions:

| Condition | Action |
| --- | --- |
| `archived` or `closed_for_applicants` | Skip |
| `has_test` or `test.required` and tests unsupported | Skip; the POST can return `test_required` (test-taking flow itself: [tests.md](tests.md)) |
| `response_url` or `adv_response_url` is external | Skip unless external forms are explicitly supported |
| Required resume is not published/owned | Stop as configuration error |
| Any product exclusion/manual-skip rule | Skip in the product layer |

`apply_alternate_url` can still be an HH response page while `response_url` identifies an external
employer flow. Prefer the explicit response fields and handle unknown combinations conservatively.

### Already applied

Do not skip solely because `relations` is non-empty, and do not assume an empty `relations` means
safe to apply. An already-applied vacancy was observed with `relations: []`; the authoritative
signal was the application POST:

```json
{
  "description": "Already applied",
  "bad_argument": "vacancy_id",
  "bad_arguments": [{"name":"vacancy_id","description":"Already applied"}],
  "errors": [{"value":"already_applied","type":"negotiations"}]
}
```

Live-verified status for this envelope is **`403`**, not `400`: HH answers business-rule
rejections on this endpoint with 403 + envelope, so a bare 401/403 status must not be decoded as
an authorization failure before the envelope values are checked. Map this to an idempotent
`already_applied`/skipped outcome, not a generic failure.

## Outcomes

| Signal | Board-neutral outcome | Batch behavior |
| --- | --- | --- |
| `201` + empty body | Replied | Continue |
| `already_applied` | Skipped/already applied | Continue |
| `test_required` | Skipped/test required | Continue unless tests are supported |
| `resume_not_found` | Configuration/user error | Stop or quarantine pipeline; do not retry |
| `limit_exceeded` | Limit reached | Stop batch; leave unattempted vacancies pending |
| `captcha_required` | Internal recoverable branch | Solve and replay within bound |
| `3xx` | External/redirect application | Skip unless that flow is implemented |
| Other 4xx | Per-vacancy rejection/error | Record and continue according to product policy |
| Connection loss after sending POST | Unknown outcome | Do not blindly retry; reconcile through negotiations first |

A POST lost after bytes were sent is not safely idempotent. Before retrying an unknown outcome,
query active negotiations for the vacancy or otherwise obtain evidence that no application exists.

## Verification after apply

The submitted application should appear in:

```http
GET /negotiations?status=active&page=0&per_page=100
```

Match by `vacancy.id`. A newly submitted application was observed with `state.id="response"`.
Treat state values as an open set rather than assuming this is the only initial state.

## Negotiation list

The endpoint is paginated and uses the shape in
[Response models](api/response-models.md#negotiation). Do not send `order_by`; the endpoint rejects
it. Follow the server ordering and page through `pages`.

Useful operations:

```http
GET /negotiations?status=active&page=<n>&per_page=100
GET /negotiations/{negotiation_id}/messages?page=<n>&per_page=<n>
```

Message sender is identified through `author.participant_type`. Read state is represented by
`read`, `viewed_by_me`, and `viewed_by_opponent`, not the older `view_state`/`source` pair.

## Follow-up mutations

Known but not fully live-verified contracts:

```http
POST /negotiations/{id}/messages
Content-Type: application/x-www-form-urlencoded

message=<text>
```

Use the same randomized 1–3 second delay and global challenge handling as apply.

Decline/cancel is documented as `DELETE /negotiations/active/{id}`. Chat hiding uses the website
route `/applicant/negotiations/trash` with cookies and XSRF. Employer/vacancy blacklisting uses API
`PUT` routes. These mutations must be revalidated and explicitly authorized before implementation;
see [Known unknowns](known-unknowns.md#mutating-workflows).
