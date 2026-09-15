"""HH test problem/solution types, the ``HhTestCapable`` protocol, and validation.

The hh-scoped contract surface (see package docstring). Types map the
hh.ru apply-page ``vacancyTests`` blob (docs/hh/tests.md §1) into board-usable
shapes; the capability protocol is the typed gate the core apply stage checks
via ``isinstance`` (D2 of the feature plan).

Naming is deliberately hh-flavored (``HhTest*``) per the feature decision: this
feature is HH-only, no other client will implement it, and the hh vocabulary
must never leak into the generic ``clients.base`` contract.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, RootModel
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import ApplyResult, ClientError, ServiceVacancyId

__all__ = [
    "HhTestCapable",
    "HhTestOption",
    "HhTestProblem",
    "HhTestSolution",
    "HhTestSolutionDocument",
    "HhTestSolveOutcome",
    "HhTestSolver",
    "HhTestTask",
    "HhTestTaskAnswer",
    "HhTestTaskAnswerPayload",
    "HhTestTaskKind",
    "HhTestUnsolved",
    "HhTestUnsolvedDocument",
    "validate_hh_test_answers",
    "validate_hh_test_solution",
]

# Task kind matrix observed on hh.ru (tests.md §1/§2). ``choice_open`` is the
# "choice plus 'Свой вариант' free-text option" shape.
HhTestTaskKind = Literal["free_text", "choice", "choice_open"]


@dataclass(frozen=True, slots=True)
class HhTestOption:
    """One candidate solution of a choice task (blob ``candidateSolutions[].id/text``)."""

    id: str
    label: str


@dataclass(frozen=True, slots=True)
class HhTestTask:
    """One screening-test question (blob ``tasks[]``)."""

    id: str  # blob numeric task id, kept as str for neutrality
    kind: HhTestTaskKind
    prompt: str  # question HTML normalized to plaintext by the client
    options: tuple[HhTestOption, ...] = ()


@dataclass(frozen=True, slots=True)
class HhTestProblem:
    """A full screening test (blob ``vacancyTests[vacancyId]``).

    ``vacancy_id`` is the file/async solver's key back into the answers file; it
    is NOT used by the submit path (a fresh blob re-read at POST time is its own
    source of truth — tests.md §11).
    """

    vacancy_id: ServiceVacancyId
    name: str
    description: str | None
    tasks: tuple[HhTestTask, ...]


@dataclass(frozen=True, slots=True)
class HhTestTaskAnswer:
    """The answer to one task (tests.md §2 encoding).

    Exactly one of the shapes applies per ``kind``:

    - ``free_text``: ``text`` (non-empty);
    - ``choice``: ``option_id`` (one of the task's options);
    - ``choice_open`` preset: ``option_id`` (``text`` may carry anything or be empty);
    - ``choice_open`` own variant: ``open=True`` + ``text`` (non-empty).
    """

    option_id: str | None = None
    open: bool = False
    text: str = ""


class HhTestTaskAnswerPayload(BaseModel):
    """The pydantic JSON shape of one task answer (external boundary).

    Twin of :class:`HhTestTaskAnswer` (the frozen domain value) — the AI
    completion output and the bulk answers file both decode through this.
    ``extra="forbid"`` so a model-invented field (``"answer"``, ``"choice"``)
    fails loudly instead of silently answering nothing.
    """

    model_config = ConfigDict(extra="forbid")

    option_id: str | None = None
    open: bool = False
    text: str = ""


class HhTestSolutionDocument(RootModel[dict[str, HhTestTaskAnswerPayload]]):
    """The whole answer document: ``task_id -> answer`` (AI output / file entry)."""


class HhTestUnsolvedDocument(BaseModel):
    """The AI's decline envelope: a single key carrying a non-empty comment.

    Twin of :class:`HhTestUnsolved` (the domain value). Only the AI solver may
    emit this shape, and only when the pipeline opts in via
    ``hh_test_solving.allow_ai_to_skip_test_when_not_enough_context``; a
    completion carrying it while the fallback is off fails normal answer-document
    validation, so the sentinel is never recognized by default. ``extra="forbid"``
    keeps it a strict single-key envelope.
    """

    model_config = ConfigDict(extra="forbid")

    not_enough_context_for_test: str


# task_id -> answer; the whole test in one map (a solver answers all tasks at once).
HhTestSolution = dict[str, HhTestTaskAnswer]


@dataclass(frozen=True, slots=True)
class HhTestUnsolved:
    """Solver outcome: the AI cannot answer from the given context.

    A *completed* solve that declines — not a failure. Carries the AI's own
    human-readable comment, which the apply stage stores as ``skip_reason`` and
    surfaces in the run output. Only producible when the pipeline enables
    ``allow_ai_to_skip_test_when_not_enough_context``.
    """

    comment: str


# A solve yields either the whole test's answers or a decline with a comment.
# Hard failures (bad JSON, invalid answers, network) stay on the ``Err`` channel.
HhTestSolveOutcome = HhTestSolution | HhTestUnsolved

# The solver seam: given the problem + the rendered prompt (the user-authored
# context vehicle: resume + vacancy + test), return answers for ALL tasks at
# once, or a decline, or an Err. File/human-style solvers may ignore the prompt
# argument and never decline.
HhTestSolver = Callable[[HhTestProblem, str], Awaitable[Result[HhTestSolveOutcome, str]]]


def _task_kind_errors(task: HhTestTask, answer: HhTestTaskAnswer) -> list[str]:
    """Validation errors for one task's answer against its kind; empty = valid."""
    errors: list[str] = []
    option_ids = {option.id for option in task.options}
    if task.kind == "free_text":
        if not answer.text.strip():
            errors.append(f"task {task.id} (free text) requires non-empty text")
        if answer.option_id is not None or answer.open:
            errors.append(f"task {task.id} (free text) accepts only text")
        return errors
    if task.kind == "choice":
        if answer.option_id not in option_ids:
            errors.append(f"task {task.id} (choice) requires one of its option ids, got {answer.option_id!r}")
        if answer.open:
            errors.append(f"task {task.id} (choice) does not accept an open answer")
        return errors
    # choice_open
    if answer.open:
        if not answer.text.strip():
            errors.append(f"task {task.id} ('Свой вариант') requires text when open")
        if answer.option_id is not None:
            errors.append(f"task {task.id} ('Свой вариант') must not carry an option id when open")
    elif answer.option_id not in option_ids:
        errors.append(f"task {task.id} (choice) requires one of its option ids, got {answer.option_id!r}")
    return errors


def validate_hh_test_answers(
    problem: HhTestProblem,
    answers: HhTestSolution,
) -> Result[HhTestSolution, str]:
    """Validate an already-decoded answer map against a problem's task surface.

    The shared semantic check: every task answered exactly once, every answer
    matching its kind, every option id still offered. It rejects **both**
    directions of drift — a submitted task the problem no longer has and a
    problem task the answer map does not cover — so the submit-time fresh-blob
    re-check catches a test that changed after solving (tests.md §11).
    """
    errors: list[str] = []
    accepted: HhTestSolution = {}
    known_ids = {task.id: task for task in problem.tasks}
    for task_id, answer in answers.items():
        task = known_ids.get(task_id)
        if task is None:
            errors.append(f"unknown task id {task_id!r}")
            continue
        task_errors = _task_kind_errors(task, answer)
        if task_errors:
            errors.extend(task_errors)
            continue
        accepted[task_id] = answer
    missing = sorted(known_ids.keys() - accepted.keys())
    if missing:
        errors.append(f"unanswered task(s): {', '.join(missing)}")
    if errors:
        return Err("; ".join(errors))
    return Ok(accepted)


def validate_hh_test_solution(
    problem: HhTestProblem,
    document: HhTestSolutionDocument,
) -> Result[HhTestSolution, str]:
    """Validate a decoded answer document against a concrete problem.

    The document is schema-validated at the boundary (pydantic
    ``extra=forbid``); this converts it to the domain answer map and applies the
    shared semantic checks in :func:`validate_hh_test_answers`.
    """
    answers = {
        task_id: HhTestTaskAnswer(option_id=payload.option_id, open=payload.open, text=payload.text)
        for task_id, payload in document.root.items()
    }
    return validate_hh_test_answers(problem, answers)


@runtime_checkable
class HhTestCapable(Protocol):
    """The typed HH-only test capability the core gates on (``isinstance``).

    Implemented by :class:`jobfucker.clients.hh.client.HHClient` only; mock and
    future boards structurally lack these methods, so the apply-stage gate is
    excluded by construction. Base ``Client`` stays 100% free of hh names.

    - ``get_vacancy_test`` — cheap web GET of the apply page + blob parse
      (tests.md §1); ``None`` = no test (or test content unrecoverable);
    - ``apply_to_vacancy_with_test`` — the website multipart flow (tests.md §2):
      fresh blob at POST time, task-id re-validation, one submission.
    """

    async def get_vacancy_test(self, vacancy_id: ServiceVacancyId) -> Result[HhTestProblem | None, ClientError]: ...

    async def apply_to_vacancy_with_test(
        self,
        *,
        resume_id: str,
        vacancy_id: ServiceVacancyId,
        message: str | None,
        solution: HhTestSolution,
    ) -> Result[ApplyResult, ClientError]: ...
