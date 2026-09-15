"""HH test prompt rendering: the user-authored prompt IS the solver context.

Follows the existing ``stages.prompts`` pattern exactly: a Jinja2 template with
YAML frontmatter (captured in the ``hh_test_solving.test_prompt`` config
content slot). The template body receives ``resume_formatted`` and
``vacancy_formatted`` (the same surfaces scoring/cover-letter prompts use) plus
the new ``test_formatted`` — the whole test, not one question at a time, so the
model keeps all task context in one prompt. ``StrictUndefined`` fails fast when
a template references a variable the code does not provide, exactly like the
scoring/apply templates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rusty_results.prelude import Result

from jobfucker.hh_tests.contract import HhTestProblem, HhTestTaskKind

if TYPE_CHECKING:
    from jobfucker.stages.prompts import PromptTemplate, VacancyPromptData

__all__ = ["format_hh_test", "render_hh_test_prompt"]


def _kind_hint(kind: HhTestTaskKind) -> str:
    """The answer shape to show next to a task (guides the model's JSON keys)."""
    match kind:
        case "free_text":
            return "free text"
        case "choice":
            return "choice"
        case "choice_open":
            return "choice or own text"


def format_hh_test(problem: HhTestProblem) -> str:
    """Format a screening test into the ``test_formatted`` injection string.

    One block: the test name + description (when present), then every task under
    its **real task id** with its options (``(id) option``), so the model sees
    the full test at once and can key its structured output by the ids the
    submission contract expects (ordinal numbering would make the model answer
    ``"1".."N"``, which then fails validation — verified live).
    """
    lines: list[str] = [f"Test: {problem.name}"]
    if problem.description:
        lines.append(f"Description:\n{problem.description}")
    for task in problem.tasks:
        lines.append(f"\nTask {task.id} ({_kind_hint(task.kind)}): {task.prompt}")
        if task.kind != "free_text":
            lines.extend(f"   ({option.id}) {option.label}" for option in task.options)
    return "\n".join(lines)


def render_hh_test_prompt(
    template: PromptTemplate,
    *,
    resume: str,
    vacancy: VacancyPromptData,
    test: HhTestProblem,
) -> Result[str, str]:
    """Render the hh test-solving prompt with resume, vacancy and test injected.

    The template's variable list is ``resume_formatted`` / ``vacancy_formatted``
    / ``test_formatted`` — nothing more; a template referencing any other
    variable fails fast via :class:`StrictUndefined`.

    ``stages.prompts`` is imported lazily: importing it at module load would
    close the cycle ``hh_tests.prompt`` → ``stages`` package init → ``apply`` →
    ``hh_tests.prompt`` (the same lazy pattern ``selector`` uses).
    """
    from jobfucker.stages.prompts import TemplateContext, format_vacancy_formatted, render_prompt

    context: TemplateContext = {
        "resume_formatted": resume,
        "vacancy_formatted": format_vacancy_formatted(vacancy),
        "test_formatted": format_hh_test(test),
    }
    return render_prompt(template, context=context)
