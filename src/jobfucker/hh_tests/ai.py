"""AI solver for HH screening tests: one chat completion per whole test.

A :data:`HhTestSolver` implementation: given the problem + the rendered
user-authored prompt (resume + vacancy + formatted test), fire a chat
completion (retried up to three attempts on failure) that returns a JSON object
of answers for ALL tasks at once (the "full test after full test" decision — all
context stays in one prompt), then validate it into a full
:data:`~jobfucker.hh_tests.contract.HhTestSolveOutcome`.

Structured output: the completion is asked to emit strict JSON
(``response_format={"type": "json_object"}``); the parsed payload is validated
task-by-task through :func:`validate_hh_test_solution`, so a hallucinated option
id, an unanswered task or malformed per-task shape collapses into a readable
``Err`` the apply stage stores as a per-vacancy failure. The answer map is keyed
by arbitrary task ids, so the strict ``json_schema`` mode cannot express it
(OpenAI Structured Outputs require fixed keys); the object mode plus local
pydantic validation is the pattern.

Optional AI decline: when the pipeline sets
``hh_test_solving.allow_ai_to_skip_test_when_not_enough_context``, the prompt
additionally offers the ``{"not_enough_context_for_test": "<comment>"}`` envelope
and the parser recognizes it, yielding :class:`HhTestUnsolved` instead of
answers. Without the opt-in the envelope is NOT offered and NOT recognized — it
fails normal answer validation, so the default behavior is unchanged.

The retry + JSON-healing + DEBUG-logging machinery is the shared generic client
in :mod:`jobfucker.ai` (:func:`~jobfucker.ai.call_with_retry` +
:func:`~jobfucker.ai.extract_json_text`); this module keeps only the
test-specific prompt contract and validation.

There is no consensus loop — captcha's plurality consensus is meaningless for
long-form test answers; a wrong answer is a per-vacancy failure, not a retried
image.

The low-level SDK call goes through the shared
:func:`~jobfucker.ai.default_completions` seam
(:data:`HhTestCompletionFn`), injectable so the solver is unit-testable against
a stub with no network — the same pattern as :mod:`jobfucker.captcha.ai`.
"""

from __future__ import annotations

import logging
from typing import Final

from pydantic import ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.ai import (
    ChatMessage,
    ChatRequest,
    CompletionsFn,
    call_with_retry,
    default_completions,
    extract_json_text,
)
from jobfucker.config import OpenAIConfig
from jobfucker.hh_tests.contract import (
    HhTestProblem,
    HhTestSolutionDocument,
    HhTestSolveOutcome,
    HhTestUnsolved,
    HhTestUnsolvedDocument,
    validate_hh_test_solution,
)

logger = logging.getLogger(__name__)

__all__ = ["HhTestAiSolver", "HhTestCompletionFn"]

# The low-level seam (shared with ``jobfucker.ai``): one chat completion with
# the instruction+prompt as the user message, returning the raw assistant text
# (the strict-JSON payload). Tests inject a stub so no network is hit.
HhTestCompletionFn = CompletionsFn

# The JSON shape the model must return, spelled out so the json_object mode has
# an unambiguous contract: a mapping task_id -> {"option_id"|"open"|"text": ...}.
_JSON_INSTRUCTION: Final = (
    "Answer the screening test. Return ONLY a JSON object mapping each task id "
    "to its answer, no other text. Use the task's declared shape exactly: a "
    '"free text" task takes {"text": "<non-empty>"} and MUST NOT use option_id '
    'or open; a "choice" task takes {"option_id": "<one of the listed option '
    'ids>"} and MUST NOT use text; a "choice or own text" task takes either '
    '{"option_id": "<one listed id>"} or {"open": true, "text": "<non-empty>"}. '
    "Never invent a task id or an option id, and never answer with an empty "
    'text. Example: {"389524036": {"option_id": "389524037"}, '
    '"389524039": {"text": "yes"}}. Every task must be answered exactly once. '
    "Prefer concise, honest answers grounded in the resume."
)

# Offered only when the pipeline opts in; appended to the user message.
_UNSOLVED_INSTRUCTION: Final = (
    "If the resume and vacancy and prompt genuinely do not contain enough information to "
    "answer the test honestly, do not guess: return exactly "
    '{"not_enough_context_for_test": "<short explanation of what is missing>"} '
    "as the whole JSON object, with a non-empty explanation. Use this fallback "
    "only when you truly cannot answer; otherwise answer every task."
)


def _build_user_prompt(rendered_prompt: str, *, allow_unsolved: bool) -> str:
    """Compose the single user message: code-owned output instruction + context.

    The prompting contract is user-message-only (no ``system`` message): the
    instruction is prepended to the rendered resume/vacancy/test context. The
    decline offer is included only when ``allow_unsolved`` is set.
    """
    instruction = f"{_JSON_INSTRUCTION}\n\n{_UNSOLVED_INSTRUCTION}" if allow_unsolved else _JSON_INSTRUCTION
    if rendered_prompt.strip():
        return f"{instruction}\n\n{rendered_prompt}"
    return instruction


def _parse_unsolved(healed: str) -> Result[HhTestUnsolved, str]:
    """Parse the decline envelope, requiring a non-empty comment."""
    try:
        document = HhTestUnsolvedDocument.model_validate_json(healed)
    except ValidationError as exc:
        return Err(f"decline document invalid ({exc})")
    comment = document.not_enough_context_for_test.strip()
    if not comment:
        return Err("decline comment is empty")
    return Ok(HhTestUnsolved(comment=comment))


def _parse_outcome(
    text: str,
    *,
    problem: HhTestProblem,
    allow_unsolved: bool,
) -> Result[HhTestSolveOutcome, str]:
    """Validate one completion into a solve outcome (answers or a decline).

    The decline envelope is only recognized when ``allow_unsolved`` is set; a
    completion carrying it otherwise fails answer-document validation, exactly
    like any other malformed payload.
    """
    healed = extract_json_text(text)
    if healed != text:
        logger.warning(
            "AI endpoint ignored response_format; extracted JSON from markdown-fenced or prose-wrapped output"
        )
    try:
        document = HhTestSolutionDocument.model_validate_json(healed)
    except ValidationError as solution_error:
        detail = text[:200] if text.strip() else "<empty>"
        if allow_unsolved:
            decline = _parse_unsolved(healed)
            if decline.is_ok:
                declined: HhTestSolveOutcome = decline.unwrap()
                return Ok(declined)
            return Err(
                f"HH test AI returned an invalid answer document ({solution_error}); "
                f"got: {detail}; decline also invalid: {decline.unwrap_err()}"
            )
        return Err(f"HH test AI returned an invalid answer document ({solution_error}); got: {detail}")

    validated = validate_hh_test_solution(problem, document)
    if validated.is_err:
        return Err(f"HH test AI answer failed validation: {validated.unwrap_err()}")
    outcome: HhTestSolveOutcome = validated.unwrap()
    return Ok(outcome)


class HhTestAiSolver:
    """A :data:`HhTestSolver` that answers a whole test via ``openai``.

    One completion per test attempt (all tasks at once), retried on failure by
    the shared :func:`~jobfucker.ai.call_with_retry`; the strict-JSON output is
    validated against the problem's task surface. A failed completion or an
    invalid payload collapses into ``Err``; the apply stage stores it
    per-vacancy. When ``allow_unsolved`` is set the model may instead return
    the decline envelope, yielding
    :class:`~jobfucker.hh_tests.contract.HhTestUnsolved`.

    Args:
        config: the main ``openai`` pipeline section (same model/key as scoring,
            per the feature decision — no dedicated ``openai_tests`` section).
        allow_unsolved: offer and recognize the
            ``not_enough_context_for_test`` decline (the
            ``hh_test_solving.allow_ai_to_skip_test_when_not_enough_context``
            config toggle).
        completion: an optional low-level completion callable. Defaults to the
            shared real SDK call; tests inject a stub so no network is hit.
    """

    def __init__(
        self,
        config: OpenAIConfig,
        *,
        allow_unsolved: bool = False,
        completion: HhTestCompletionFn | None = None,
    ) -> None:
        self._config: OpenAIConfig = config
        self._allow_unsolved: bool = allow_unsolved
        self._completion: HhTestCompletionFn = completion if completion is not None else default_completions(config)

    async def __call__(self, problem: HhTestProblem, prompt: str) -> Result[HhTestSolveOutcome, str]:
        """Solve (or decline) the whole test, validating the result.

        The completion is retried up to three times with exponential backoff by
        :func:`~jobfucker.ai.call_with_retry`; both seam exceptions and
        validation failures are retried. When the budget is exhausted the last
        failure is returned.
        """
        user_prompt = _build_user_prompt(prompt, allow_unsolved=self._allow_unsolved)
        request = ChatRequest(
            model=self._config.model,
            messages=(ChatMessage(role="user", content=user_prompt),),
            reasoning_effort=self._config.reasoning_effort,
            response_format="json_object",
        )
        outcome = await call_with_retry(
            self._completion,
            request,
            parse=lambda text: _parse_outcome(text, problem=problem, allow_unsolved=self._allow_unsolved),
        )
        if outcome.is_err:
            logger.warning("HH test AI solving failed for %s: %s", problem.vacancy_id, outcome.unwrap_err())
        return outcome
