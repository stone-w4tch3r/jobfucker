"""Answers-file solver: the async/bulk transport for HH test solving.

A :data:`HhTestSolver` implementation backed by a user-authored JSON answers
file (the ``apply --test-answers FILE`` / ``hh-tests dump`` flow): one entry per
vacancy external id, each mapping task id -> answer. The file is an external
boundary — decoded through pydantic
(:class:`~jobfucker.hh_tests.contract.HhTestSolutionDocument`) at read time,
then validated against each problem's task surface at solve time.

The dump side (``hh-tests dump``) writes the matching problems JSON; the file
is edited by a human or filled by an offline AI pass, then ``apply
--test-answers FILE`` wires this solver so the apply stage does not probe or
solve inline.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import RootModel, ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.hh_tests.contract import (
    HhTestProblem,
    HhTestSolutionDocument,
    HhTestSolveOutcome,
    validate_hh_test_solution,
)

__all__ = ["HhTestFileSolver"]


class _HhTestAnswersFile(RootModel[dict[str, HhTestSolutionDocument]]):
    """The whole answers file: ``vacancy_id -> {task_id -> answer}``.

    Each vacancy value is the same document type the AI solver decodes, so the
    per-task schema is defined once.
    """


def _load_answers(path: Path) -> Result[_HhTestAnswersFile, str]:
    """Read + pydantic-validate the answers file, or a clear ``Err``.

    The per-problem validation against the real task surface happens per solve
    call (the file cannot know each test's current option ids).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Err(f"Cannot read --test-answers file {path}: {exc}")
    try:
        document = _HhTestAnswersFile.model_validate_json(raw)
    except ValidationError as exc:
        return Err(f"Invalid --test-answers file {path}: {exc}")
    return Ok(document)


class HhTestFileSolver:
    """A :data:`HhTestSolver` that reads answers from a validated JSON file.

    Args:
        path: the answers file path (external boundary, pydantic-validated).
          ``~`` expands to the user's home.
    """

    def __init__(self, path: Path) -> None:
        self._path: Path = path.expanduser()

    async def __call__(self, problem: HhTestProblem, prompt: str) -> Result[HhTestSolveOutcome, str]:
        """Resolve the problem's answers from the file entry for its vacancy.

        ``prompt`` is ignored (a file solver has no prompt context). A missing
        vacancy entry or a per-task mismatch with the current problem surface is
        a clear ``Err`` (the apply stage stores it per-vacancy), never a guess.
        """
        del prompt
        loaded = _load_answers(self._path)
        if loaded.is_err:
            return Err(loaded.unwrap_err())
        document = loaded.unwrap().root.get(str(problem.vacancy_id))
        if document is None:
            return Err(f"No answers for vacancy {problem.vacancy_id} in {self._path}")
        validated = validate_hh_test_solution(problem, document)
        if validated.is_err:
            return Err(f"Answers for {problem.vacancy_id} are invalid: {validated.unwrap_err()}")
        outcome: HhTestSolveOutcome = validated.unwrap()
        return Ok(outcome)
