"""Prompt loading + Jinja2 rendering with YAML frontmatter (Phase 5A, task 5.3).

Per the architecture §3.2 / §4, prompt templates are **Jinja2 files with YAML
frontmatter**: a leading ``---``-delimited YAML block holds params (metadata),
and the Jinja2 body is what gets rendered with the injected content (resume,
vacancy). ``config.py`` already captures the *raw* template text
(``scoring_prompt`` / ``apply_prompt``); this module parses that text and
renders it.

The templates are the source of truth for the variable list. Both scoring and
apply templates expect exactly two variables: ``resume_formatted`` and
``vacancy_formatted`` (single strings). :class:`StrictUndefined` makes a
missing variable fail fast — if the user edits a template to reference a
variable the code does not provide, rendering returns a clear ``Err``.

This module is **pure** — it only parses and renders strings. It performs no
I/O beyond :func:`load_prompt_template` (which reads a file) and never calls the
network. It is unit-tested for frontmatter parsing + injection.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined, select_autoescape
from jinja2.exceptions import TemplateError
from rusty_results.prelude import Err, Ok, Result

__all__ = [
    "PromptInputs",
    "PromptParams",
    "PromptTemplate",
    "TemplateContext",
    "VacancyPromptData",
    "format_vacancy_formatted",
    "load_prompt_template",
    "parse_prompt_template",
    "render_apply_prompt",
    "render_prompt",
    "render_scoring_prompt",
]

# Parsed YAML frontmatter params. This is the dynamic YAML/JSON boundary —
# ``object`` values are the untyped template metadata, narrowed by callers that
# need specific params (none do today; params are authored metadata only).
# Alias *values* are not flagged by the annotation-only object linter; the
# alias itself names the single, documented boundary type.
type PromptParams = Mapping[str, object]

# The values a prompt body takes from the engine (resume, vacancy fields, score,
# reasoning). ``object`` is the templating boundary — Jinja2 renders any value.
type TemplateContext = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """A parsed prompt: YAML frontmatter params + a Jinja2 body."""

    params: PromptParams
    body: str


@dataclass(frozen=True, slots=True)
class VacancyPromptData:
    """The vacancy fields injected into a prompt body."""

    title: str
    company: str | None
    description: str
    salary: str | None


@dataclass(frozen=True, slots=True)
class PromptInputs:
    """The render inputs a stage needs: resume contents + parsed prompt template.

    Bundled so stage entry points stay within the ``max-args`` limit and so the
    engine controller builds one object per stage (score vs apply prompt).
    """

    resume: str
    prompt: PromptTemplate


# --- Frontmatter parsing -----------------------------------------------------
def _split_frontmatter(text: str) -> Result[tuple[PromptParams, str], str]:
    """Split ``--- ... ---`` frontmatter from the Jinja2 body.

    The template may omit frontmatter entirely (then params are empty). A
    malformed YAML block is an ``Err``.
    """
    stripped = text.lstrip("\ufeff")
    if not stripped.startswith("---"):
        return Ok(({}, stripped))
    # The first line is the opening ``---``; the closing ``---`` is a line that
    # is exactly ``---`` (allow trailing whitespace) before the body.
    lines = stripped.splitlines(keepends=True)
    first = lines[0].strip()
    if first != "---":
        return Ok(({}, stripped))
    end: int | None = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end = i
            break
    if end is None:
        return Err("Prompt template has an opening '---' but no closing '---' frontmatter delimiter")
    frontmatter_lines = lines[1:end]
    body_lines = lines[end + 1 :]

    params = _parse_yaml_frontmatter("".join(frontmatter_lines))
    if params.is_err:
        return Err(params.unwrap_err())
    body_text = "".join(body_lines)
    return Ok((params.unwrap(), body_text))


def _safe_load_yaml(text: str) -> object:  # lint-ignore[restricted-object]: YAML boundary
    """Parse a YAML document into an untyped ``object`` (dynamic boundary)."""
    return yaml.safe_load(text)  # type: ignore[reportAny]  # rationale: PyYAML returns Any; object is the dynamic boundary


def _parse_yaml_frontmatter(text: str) -> Result[PromptParams, str]:
    """Parse the frontmatter YAML into params, or a clear ``Err``."""
    try:
        parsed = _safe_load_yaml(text)
    except yaml.YAMLError as exc:
        return Err(f"Malformed prompt frontmatter YAML: {exc}")
    if parsed is None:
        return Ok({})
    if not isinstance(parsed, dict):
        return Err("Prompt frontmatter must be a YAML mapping")
    narrowed: TemplateContext = {}  # lint-ignore[raw-dict]: frontmatter mapping
    for key in parsed:  # type: ignore[reportUnknownVariableType]  # rationale: dict from dynamic YAML; keys narrowed to str
        if isinstance(key, str):
            narrowed[key] = parsed[key]
    return Ok(narrowed)


# --- Public API --------------------------------------------------------------
def parse_prompt_template(text: str) -> Result[PromptTemplate, str]:
    """Parse raw template text (frontmatter + body) into a :class:`PromptTemplate`."""
    split = _split_frontmatter(text)
    if split.is_err:
        return Err(split.unwrap_err())
    params, body = split.unwrap()
    return Ok(PromptTemplate(params=params, body=body))


def load_prompt_template(path: Path) -> Result[PromptTemplate, str]:
    """Read and parse a prompt template file at ``path``."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return Err(f"Could not read prompt template {path}: {exc}")
    parsed = parse_prompt_template(raw)
    if parsed.is_err:
        return Err(parsed.unwrap_err())
    return Ok(parsed.unwrap())


def render_prompt(template: PromptTemplate, *, context: TemplateContext) -> Result[str, str]:
    """Render a template body with the given context into text.

    Args:
        template: the parsed prompt template.
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
        compiled = env.from_string(template.body)
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
    template: PromptTemplate,
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
    return render_prompt(template, context=context)


def render_apply_prompt(
    template: PromptTemplate,
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
    return render_prompt(template, context=context)
