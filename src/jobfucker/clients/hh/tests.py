"""HH screening-test web service: problem fetch + apply-with-answers submission.

Implements the website half of the ``HhTestCapable`` contract
(``jobfucker.hh_tests.contract``): both methods are website flows authorized by
**web cookies** (``hhul``/``hhrole``/``hhtoken``/``_xsrf`` — Bearer does not
authorize ``hh.ru``), per the verified contract in ``docs/hh/tests.md``.

- ``get_vacancy_test`` — GET the apply page, extract the ``HH-Lux-InitialState``
  template blob, map ``vacancyTests[vacancyId]`` onto :class:`HhTestProblem`.
- ``apply_with_test`` — the "one POST = apply + test answers" flow: fresh page
  GET (re-stamps ``startTime``), already-applied detection, task-id
  re-validation, multipart POST to ``/applicant/vacancy_response/popup``.
  ``Ok(None)`` signals "the test no longer exists on the fresh page while the
  vacancy stays open" — ``HHClient`` then routes **once** through the standard
  application flow, whose own preflight re-checks archived/closed. A removed
  test is not a skip; it is a plain vacancy now.

The 200-270 KB initial-state blob is decoded through pydantic models
(``extra="ignore"`` — the blob carries hundreds of unrelated keys) so every
field read below is typed; no manual dict narrowing.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import (
    ApplyError,
    ApplyFailed,
    ApplyResult,
    ApplySkip,
    ApplySkipped,
    ApplySucceeded,
    AuthError,
    ClientError,
    ConfigurationError,
    ProtocolError,
    ServiceVacancyId,
)
from jobfucker.clients.hh.search import DescriptionParser
from jobfucker.clients.hh.transport import FormFields, HHTransport
from jobfucker.hh_tests.contract import (
    HhTestOption,
    HhTestProblem,
    HhTestSolution,
    HhTestTask,
    HhTestTaskKind,
    validate_hh_test_answers,
)

logger = logging.getLogger(__name__)

__all__ = ["HhTestService"]

_APPLY_PAGE_PATH: Final = "/applicant/vacancy_response"
_SUBMIT_PATH: Final = "/applicant/vacancy_response/popup"
_HTTP_OK: Final = 200
_HTTP_UNAUTHORIZED: Final = frozenset({401, 403})
_HTTP_NOT_FOUND: Final = 404
_HTTP_REDIRECT_MIN: Final = 300
_HTTP_REDIRECT_MAX: Final = 400

# The ``<template id="HH-Lux-InitialState">`` blob is HTML-escaped (``&#34;``
# for ``"``) and carries extra attributes on the tag; the group is the raw
# escaped JSON (docs/hh/tests.md §1 raw-HTML gotcha).
_BLOB_PATTERN: Final = re.compile(r'<template[^>]*id="HH-Lux-InitialState">(.*?)</template>', re.S)


class _HhBlobModel(BaseModel):
    """Base for initial-state blob models: camelCase JSON, ignore unrelated keys.

    ``extra="ignore"`` because the real blob carries hundreds of unrelated
    keys; every field actually read below is declared. ``coerce_numbers_to_str``
    accounts for blob ids that arrive as JSON numbers in some positions and as
    strings in others (tests.md §1).
    """

    model_config = ConfigDict(
        extra="ignore",
        alias_generator=to_camel,
        populate_by_name=True,
        coerce_numbers_to_str=True,
    )


class _HhBlobCandidate(_HhBlobModel):
    """One ``candidateSolutions[]`` entry."""

    id: str
    text: str


class _HhBlobTask(_HhBlobModel):
    """One ``tasks[]`` entry (stringly ``multiple``/``open``, tests.md §1)."""

    id: str
    description: str | None = None
    multiple: str = "false"
    open: str = "false"
    candidate_solutions: list[_HhBlobCandidate] = []


class _HhBlobResumeAttributes(_HhBlobModel):
    """The resume ``_attributes`` block holding the apply POST's ``hash``.

    ``resumes`` is keyed by the short numeric resume id, while the configured
    ``service.hh.resume_id`` is the long ``hash`` (the POST value) — so the
    configured id is matched against ``hash``/``id``, never against the map key
    (verified live).
    """

    hash: str | None = None
    id: str | None = None


class _HhBlobResume(_HhBlobModel):
    """One ``resumes[resumeId]`` entry (``_attributes`` is alias-mapped)."""

    attributes: _HhBlobResumeAttributes | None = Field(default=None, alias="_attributes")


class _HhBlobStatus(_HhBlobModel):
    """One ``applicantVacancyResponseStatuses[vacancyId]`` entry (the fields used)."""

    already_applied: bool = False
    response_impossible: bool = False
    resumes: dict[str, _HhBlobResume] = {}  # lint-ignore[raw-dict]: JSON object mapping
    # Short numeric ids of the resumes applicable to THIS vacancy (tests.md §1);
    # a resume in ``resumes`` but not here is not selectable for the apply POST.
    unused_resume_ids: list[str] = []


class _HhBlobTest(_HhBlobModel):
    """One ``vacancyTests[vacancyId]`` entry (the test itself)."""

    uid_pk: str
    guid: str
    name: str
    description: str | None = None
    required: str = "false"
    start_time: str
    tasks: list[_HhBlobTask] = []


class _HhInitialState(_HhBlobModel):
    """The subset of the ``HH-Lux-InitialState`` blob this service needs."""

    vacancy_tests: dict[str, _HhBlobTest] = {}  # lint-ignore[raw-dict]: JSON object mapping
    applicant_vacancy_response_statuses: dict[str, _HhBlobStatus] = {}  # lint-ignore[raw-dict]: JSON object mapping


class HhWebApplyError(_HhBlobModel):
    """The business-error envelope of the apply POST (docs/hh/tests.md §3).

    Inherits the camelCase alias mapping so the observed ``redirectUrl`` key
    lands on ``redirect_url`` (tests.md §3).
    """

    error: str | None = None
    redirect_url: str | None = None


class _HhWebSuccess(BaseModel):
    """The success body of the apply POST (``success`` is stringly ``"true"``)."""

    model_config = ConfigDict(extra="ignore")

    success: bool = False


def _plaintext(markup: str) -> str:
    """Normalize test HTML (questions + description) to plaintext."""
    parser = DescriptionParser()
    parser.feed(markup)
    parser.close()
    return parser.text()


def _is_true(value: str) -> bool:
    """The blob's stringly booleans (``"true"``/``"false"``)."""
    return value == "true"


class HhTestService:
    """Website-only HH test fetch + apply-with-answers submission."""

    def __init__(self, transport: HHTransport) -> None:
        self._transport: HHTransport = transport

    async def get_vacancy_test(self, vacancy_id: ServiceVacancyId) -> Result[HhTestProblem | None, ClientError]:
        """Fetch the apply page and map the embedded test, or ``None`` (no test)."""
        state_result = await self._page_blob(vacancy_id)
        if state_result.is_err:
            return Err(state_result.unwrap_err())
        state = state_result.unwrap()
        if state is None:
            return Ok(None)
        entry = state.vacancy_tests.get(str(vacancy_id))
        if entry is None:
            return Ok(None)
        return Ok(self._to_problem(vacancy_id, entry))

    def _to_problem(self, vacancy_id: ServiceVacancyId, entry: _HhBlobTest) -> HhTestProblem:
        """Map one validated ``vacancyTests[vacancyId]`` entry onto the contract."""
        tasks = tuple(self._to_task(task) for task in entry.tasks)
        description = _plaintext(entry.description) if entry.description is not None else None
        return HhTestProblem(vacancy_id=vacancy_id, name=entry.name, description=description, tasks=tasks)

    def _to_task(self, task: _HhBlobTask) -> HhTestTask:
        """Map one ``tasks[]`` entry, resolving the kind matrix (tests.md §1)."""
        options = tuple(HhTestOption(id=candidate.id, label=candidate.text) for candidate in task.candidate_solutions)
        open_ = _is_true(task.open)
        if _is_true(task.multiple):
            # Unobserved in the wild (tests.md §10). REMOVEME — delete this
            # fallback (fail loudly instead) once a live checklist task is
            # verified in the real world.
            logger.warning(
                "REMOVEME: HH checkbox task observed (multiple=true) for task %s; solving as single choice — "
                "verify against a real checklist test and remove this branch",
                task.id,
            )
        if options and open_:
            kind: HhTestTaskKind = "choice_open"
        elif options:
            kind = "choice"
        else:
            kind = "free_text"
        prompt = _plaintext(task.description) if task.description else ""
        return HhTestTask(id=task.id, kind=kind, prompt=prompt, options=options)

    # --- apply with answers --------------------------------------------------
    async def apply_with_test(  # noqa: PLR0911 - the observed submission contract has many distinct branches
        self,
        *,
        resume_id: str,
        vacancy_id: ServiceVacancyId,
        message: str | None,
        solution: HhTestSolution,
    ) -> Result[ApplyResult | None, ClientError]:
        """Submit apply + answers in one website POST (fresh blob, tests.md §2-3).

        ``Ok(None)`` signals that the fresh page no longer carries the test while
        staying applyable (or the page carried no initial state): the caller
        falls back once to the standard apply flow.
        """
        state_result = await self._page_blob(vacancy_id)
        if state_result.is_err:
            return Err(state_result.unwrap_err())
        state = state_result.unwrap()
        if state is None:
            return Ok(None)
        status = state.applicant_vacancy_response_statuses.get(str(vacancy_id))
        if status is not None and (status.already_applied or status.response_impossible):
            return Ok(
                ApplySkipped(
                    ApplySkip(reason="already_applied", text=f"HH vacancy {vacancy_id} was already applied to")
                )
            )
        entry = state.vacancy_tests.get(str(vacancy_id))
        if entry is None:
            return Ok(None)

        # The fresh blob is the source of truth: validate the solved answers
        # against its *current* task surface (both directions + option ids) so
        # a test edited after solving is refused, never half-submitted.
        fresh = self._to_problem(vacancy_id, entry)
        revalidated = validate_hh_test_answers(fresh, solution)
        if revalidated.is_err:
            return Ok(
                ApplyFailed(
                    ApplyError(
                        text=f"HH test for {vacancy_id} changed after solving: {revalidated.unwrap_err()}; re-solve",
                        code="test_changed",
                    )
                )
            )

        resume_hash_result = self._resume_hash(state, vacancy_id, resume_id)
        if resume_hash_result.is_err:
            return Err(ConfigurationError(message=resume_hash_result.unwrap_err()))
        resume_hash = resume_hash_result.unwrap()
        if resume_hash is None:
            return Err(ConfigurationError(message=f"HH apply page exposes no resume hash for resume {resume_id}"))

        xsrf = self._transport.cookie_value("_xsrf")
        if xsrf is None:
            return Err(AuthError(message="HH apply-with-test requires the _xsrf website cookie"))
        form = self._build_form(
            vacancy_id=vacancy_id,
            entry=entry,
            resume_hash=resume_hash,
            solution=solution,
            message=message,
            xsrf=xsrf,
        )

        referer = f"https://hh.ru{_APPLY_PAGE_PATH}?vacancyId={vacancy_id}"
        response_result = await self._transport.post_website_multipart(
            _SUBMIT_PATH,
            fields=form,
            xsrf=xsrf,
            referer=referer,
        )
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        return self._classify_submission(response_result.unwrap(), vacancy_id)

    def _resume_hash(
        self,
        state: _HhInitialState,
        vacancy_id: ServiceVacancyId,
        resume_id: str,
    ) -> Result[str | None, str]:
        """Read the resume ``_attributes.hash`` from the status blob (tests.md §1).

        ``status.resumes`` is keyed by the short numeric resume id, but the
        configured ``resume_id`` is the long ``_attributes.hash``, so the entry
        is found by matching the configured id against each entry's
        ``hash``/``id``. The POST value is always the entry's ``hash``. A
        configured resume absent from the applicable resumes
        (``unusedResumeIds``, the per-vacancy selectable set — tests.md §1)
        yields ``None``; a resume the page lists but does not offer for this
        vacancy is never submitted.
        """
        status = state.applicant_vacancy_response_statuses.get(str(vacancy_id))
        if status is None:
            return Err("HH apply page status blob has no entry for this vacancy")
        applicable = set(status.unused_resume_ids)
        for short_id, resume in status.resumes.items():
            attributes = resume.attributes
            if attributes is None:
                continue
            if resume_id not in (attributes.hash, attributes.id):
                continue
            applicable_here = short_id in applicable or (attributes.id is not None and attributes.id in applicable)
            if applicable_here and attributes.hash is not None:
                return Ok(attributes.hash)
        return Ok(None)

    async def _page_blob(self, vacancy_id: ServiceVacancyId) -> Result[_HhInitialState | None, ClientError]:  # noqa: PLR0911 - status/blob branches per the observed page contract
        """GET the apply page and decode the initial-state blob (or ``None``).

        A 404 (unknown/closed vacancy page) collapses into ``Ok(None)``; a
        401/403 is an auth signal; any other status or a malformed blob is a
        protocol error.
        """
        response_result = await self._transport.get_website(
            _APPLY_PAGE_PATH,
            params=(("vacancyId", str(vacancy_id)),),
        )
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        response = response_result.unwrap()
        if response.status_code == _HTTP_NOT_FOUND:
            return Ok(None)
        if response.status_code in _HTTP_UNAUTHORIZED:
            return Err(AuthError(message="HH rejected authorization on the vacancy response page"))
        if _HTTP_REDIRECT_MIN <= response.status_code < _HTTP_REDIRECT_MAX:
            # Redirects are disabled; an apply page that redirects means the
            # website session is gone (login interstitial), not a closed vacancy.
            return Err(AuthError(message="HH redirected the vacancy response page (website session expired)"))
        if response.status_code != _HTTP_OK:
            return Err(
                ProtocolError(
                    message=f"HH vacancy response page returned {response.status_code}",
                    status=response.status_code,
                )
            )
        match = _BLOB_PATTERN.search(response.text)
        if match is None:
            return Ok(None)
        decoded = html.unescape(match.group(1))
        try:
            return Ok(_HhInitialState.model_validate_json(decoded))
        except ValidationError as exc:
            return Err(ProtocolError(message=f"HH vacancy response page blob failed validation: {exc}"))

    def _build_form(  # noqa: PLR0913 - full multipart field contract (tests.md §2)
        self,
        *,
        vacancy_id: ServiceVacancyId,
        entry: _HhBlobTest,
        resume_hash: str,
        solution: HhTestSolution,
        message: str | None,
        xsrf: str,
    ) -> FormFields:
        """Build the complete multipart field set per tests.md §2."""
        fields: FormFields = (
            ("_xsrf", xsrf),
            ("uidPk", entry.uid_pk),
            ("guid", entry.guid),
            ("startTime", entry.start_time),
            ("testRequired", entry.required),
            ("vacancy_id", str(vacancy_id)),
            ("resume_hash", resume_hash),
            ("ignore_postponed", "true"),
            ("incomplete", "false"),
            ("mark_applicant_visible_in_vacancy_country", "false"),
            ("country_ids", "[]"),
            ("letter", message or ""),
            ("lux", "true"),
            ("withoutTest", "no"),  # never "yes": tests.md §6
            ("hhtmFromLabel", ""),
            ("hhtmSourceLabel", ""),
        )
        task_open = {task.id: _is_true(task.open) for task in entry.tasks}
        task_fields: FormFields = ()
        for task_id, answer in solution.items():
            if answer.open:
                task_fields += ((f"task_{task_id}", "open"), (f"task_{task_id}_text", answer.text))
            elif answer.option_id is not None:
                task_fields += ((f"task_{task_id}", answer.option_id),)
                # choice_open tasks pair a chosen option with an (empty) text field.
                if task_open.get(task_id, False):
                    task_fields += ((f"task_{task_id}_text", ""),)
            else:
                task_fields += ((f"task_{task_id}_text", answer.text),)
        return fields + task_fields

    def _classify_submission(  # noqa: PLR0911 - observed response contract (tests.md §3)
        self,
        response: httpx.Response,
        vacancy_id: ServiceVacancyId,
    ) -> Result[ApplyResult | None, ClientError]:
        """Classify the popup POST outcome per docs/hh/tests.md §3.

        A 401/403 (the stale/missing ``_xsrf`` error page) is an **auth-class**
        failure: returned as ``Err(AuthError)`` so the stage stops the batch
        instead of deciding every test vacancy as an error.
        """
        envelope = _web_error_envelope(response)
        if envelope is not None and envelope.error:
            return Ok(self._classify_web_error(envelope, vacancy_id))
        if response.status_code == _HTTP_OK:
            if not response.content:
                return Ok(ApplySucceeded())
            try:
                success = _HhWebSuccess.model_validate_json(response.content)
            except ValidationError as exc:
                return Ok(
                    ApplyFailed(
                        ApplyError(text=f"HH test apply success body is invalid JSON: {exc}", code="web_apply_error")
                    )
                )
            if success.success:
                return Ok(ApplySucceeded())
            return Ok(
                ApplyFailed(
                    ApplyError(text="HH test apply success body carried an unexpected payload", code="web_apply_error")
                )
            )
        if response.status_code in _HTTP_UNAUTHORIZED:
            return Err(AuthError(message="HH rejected authorization during test apply"))
        return Ok(
            ApplyFailed(
                ApplyError(
                    text=f"HH test apply returned unexpected status {response.status_code}", code="web_apply_error"
                )
            )
        )

    def _classify_web_error(self, envelope: HhWebApplyError, vacancy_id: ServiceVacancyId) -> ApplyResult:
        """Map a documented business error onto the contract outcome."""
        # Observed envelopes are hyphenated (`test-required`, `resume-incomplete`),
        # camelCase (`alreadyApplied`), and occasionally snake_case — collapse all.
        value = (envelope.error or "").casefold().replace("_", "").replace("-", "")
        if value == "alreadyapplied":
            return ApplySkipped(
                ApplySkip(reason="already_applied", text=f"HH vacancy {vacancy_id} was already applied to")
            )
        if value == "testrequired":
            return ApplyFailed(
                ApplyError(text=f"HH vacancy {vacancy_id} rejected the test apply: test-required", code="test_required")
            )
        if value == "letterrequired":
            return ApplyFailed(
                ApplyError(
                    text=f"HH vacancy {vacancy_id} requires a non-empty cover letter (letter-required)",
                    code="letter_required",
                )
            )
        if value == "resumeincomplete":
            target = envelope.redirect_url if envelope.redirect_url is not None else "the resume edit page"
            return ApplyFailed(
                ApplyError(text=f"HH vacancy {vacancy_id}: resume is incomplete ({target})", code="resume_incomplete")
            )
        return ApplyFailed(ApplyError(text=f"HH test apply rejected: {envelope.error}", code="web_apply_error"))


def _web_error_envelope(response: httpx.Response) -> HhWebApplyError | None:
    """Parse the website business-error envelope, or ``None`` when absent."""
    try:
        return HhWebApplyError.model_validate_json(response.content)
    except ValidationError:
        return None
