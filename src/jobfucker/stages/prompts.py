"""Prompt loading + Jinja2 rendering (Phase 5A, task 5.3).

Prompt templates are **Jinja2 files**: the whole file text is the render body.
``config.py`` captures the *raw* template text (``scoring_prompt`` /
``apply_prompt``); this module renders it with the injected content (resume,
vacancy).

The templates are the source of truth for the variable list. Both scoring and
apply templates expect exactly two variables: ``resume_formatted`` and
``vacancy_formatted`` (single strings). :class:`StrictUndefined` makes a
missing variable fail fast — if the user edits a template to reference a
variable the code does not provide, rendering returns a clear ``Err``.

This module is **pure** — it only renders strings. It performs no I/O and never
calls the network. It is unit-tested for injection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from jinja2 import Environment, StrictUndefined, select_autoescape
from jinja2.exceptions import TemplateError
from rusty_results.prelude import Err, Ok, Result

__all__ = [
    "PromptInputs",
    "TemplateContext",
    "VacancyPromptData",
    "format_vacancy_formatted",
    "render_apply_prompt",
    "render_prompt",
    "render_scoring_prompt",
]

# The values a prompt body takes from the engine (resume, vacancy fields, score,
# reasoning). ``object`` is the templating boundary — Jinja2 renders any value.
type TemplateContext = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class VacancyPromptData:
    """The vacancy fields injected into a prompt body."""

    title: str
    company: str | None
    description: str
    salary: str | None


@dataclass(frozen=True, slots=True)
class PromptInputs:
    """The render inputs a stage needs: resume contents + the prompt text.

    Bundled so stage entry points stay within the ``max-args`` limit and so the
    engine controller builds one object per stage (score vs apply prompt).
    """

    resume: str
    prompt: str


def render_prompt(prompt: str, *, context: TemplateContext) -> Result[str, str]:
    """Render a prompt body with the given context into text.

    Args:
        prompt: the raw Jinja2 prompt text.
        context: values injected into the Jinja2 body (resume, vacancy fields,
            score, reasoning, ...).

    Returns:
        ``Ok(rendered_text)`` or an ``Err`` with the template error message.
    """
    # Plain-text LLM prompts: no HTML autoescaping (would corrupt content). We
    # still pass ``select_autoescape`` so Bandit sees an explicit policy, and
    # escaping only applies to HTML-ish template *names* (never our ``from_string``
    # body), keeping injected resume/vacancy text raw. ``StrictUndefined`` makes
    # a missing variable fail fast (clear ``Err``) so the user knows the prompt
    # references a variable the code does not provide.
    env = Environment(
        autoescape=select_autoescape(("html", "htm", "xml")),
        trim_blocks=True,
        lstrip_blocks=True,
        undefined=StrictUndefined,
    )
    try:
        compiled = env.from_string(prompt)
        rendered = compiled.render(**dict(context))  # type: ignore[reportArgumentType]  # rationale: Jinja2 renders object values; boundary to templating
    except TemplateError as exc:
        return Err(f"Prompt render failed: {exc}")
    return Ok(rendered)


def _format_vacancy(vacancy: VacancyPromptData) -> str:
    """Format a vacancy into a single string injected as ``vacancy_formatted``.

    The template's ``{{ vacancy_formatted }}`` is the sole vacancy surface;
    the code owns the formatting, the template owns placement.
    """
    lines: list[str] = [f"Title: {vacancy.title}"]
    if vacancy.company:
        lines.append(f"Company: {vacancy.company}")
    if vacancy.salary:
        lines.append(f"Salary: {vacancy.salary}")
    lines.append(f"Description:\n{vacancy.description}")
    return "\n".join(lines)


def format_vacancy_formatted(vacancy: VacancyPromptData) -> str:
    """Format a vacancy into the ``vacancy_formatted`` injection string.

    Public alias of the internal formatter so board-scoped prompt extensions
    (e.g. ``jobfucker.hh_tests.prompt``) reuse the same single vacancy surface
    the scoring/apply templates get.
    """
    return _format_vacancy(vacancy)


def render_scoring_prompt(
    prompt: str,
    *,
    resume: str,
    vacancy: VacancyPromptData,
) -> Result[str, str]:
    """Render the scoring prompt with resume + vacancy injected.

    The template variables are the source of truth: the context provides
    exactly ``resume_formatted`` and ``vacancy_formatted`` — nothing more.
    A template referencing any other variable fails fast via
    :class:`StrictUndefined`.
    """
    context: TemplateContext = {  # lint-ignore[raw-dict]: template context mapping
        "resume_formatted": resume,
        "vacancy_formatted": _format_vacancy(vacancy),
    }
    return render_prompt(prompt, context=context)


def render_apply_prompt(
    prompt: str,
    *,
    resume: str,
    vacancy: VacancyPromptData,
) -> Result[str, str]:
    """Render the cover-letter prompt with resume + vacancy injected.

    The apply template's variable list is ``resume_formatted`` and
    ``vacancy_formatted`` only (no score/reasoning — the template does not use
    them). A template referencing any other variable fails fast via
    :class:`StrictUndefined`.
    """
    context: TemplateContext = {  # lint-ignore[raw-dict]: template context mapping
        "resume_formatted": resume,
        "vacancy_formatted": _format_vacancy(vacancy),
    }
    return render_prompt(prompt, context=context)
