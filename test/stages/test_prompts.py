"""Phase 5A (task 5.3): unit tests for prompt rendering.

Pure unit tests (plain pytest): render a Jinja2 prompt body with injected
resume / vacancy content.
"""

from __future__ import annotations

from jobfucker.stages.prompts import (
    PromptInputs,
    VacancyPromptData,
    render_apply_prompt,
    render_prompt,
    render_scoring_prompt,
)

SCORE_TEMPLATE = """Rate the match between this resume and vacancy.

Resume:
{{ resume_formatted }}

Vacancy: {{ vacancy_formatted }}
"""

APPLY_TEMPLATE = """Write a cover letter using the resume and vacancy.

Resume:
{{ resume_formatted }}

Vacancy: {{ vacancy_formatted }}
"""


def _vacancy() -> VacancyPromptData:
    return VacancyPromptData(
        title="Senior Backend",
        company="ACME",
        description="Build FastAPI services.",
        salary="200k",
    )


# --- rendering ------------------------------------------------------------------
def test_render_scoring_prompt_injects_resume_and_vacancy() -> None:
    """The scoring body receives resume_formatted + vacancy_formatted."""
    rendered = render_scoring_prompt(SCORE_TEMPLATE, resume="My CV", vacancy=_vacancy())
    assert rendered.is_ok
    text = rendered.unwrap()
    assert "My CV" in text
    assert "Senior Backend" in text
    assert "ACME" in text
    assert "Build FastAPI services." in text


def test_render_apply_prompt_injects_resume_and_vacancy() -> None:
    """The apply body receives resume_formatted + vacancy_formatted (no score/reasoning)."""
    rendered = render_apply_prompt(
        APPLY_TEMPLATE,
        resume="My CV",
        vacancy=_vacancy(),
    )
    assert rendered.is_ok
    text = rendered.unwrap()
    assert "My CV" in text
    assert "Senior Backend" in text


def test_render_prompt_with_empty_company_is_ok() -> None:
    """A vacancy with no company renders without a company line, not an error."""
    vacancy = VacancyPromptData(title="X", company=None, description="", salary=None)
    rendered = render_scoring_prompt(SCORE_TEMPLATE, resume="CV", vacancy=vacancy)
    assert rendered.is_ok
    assert "Title: X" in rendered.unwrap()


def test_render_missing_variable_is_err() -> None:
    """A template referencing a variable not in the context fails fast."""
    rendered = render_scoring_prompt(
        "Score {{ resume_formatted }} and {{ unknown_var }}",
        resume="CV",
        vacancy=_vacancy(),
    )
    assert rendered.is_err
    assert "render failed" in rendered.unwrap_err().lower()


def test_render_invalid_jinja_is_err() -> None:
    """A broken Jinja2 body is a clear Err."""
    rendered = render_prompt("Unclosed: {{ resume_formatted", context={"resume_formatted": "x"})
    assert rendered.is_err
    assert "render failed" in rendered.unwrap_err().lower()


def test_prompt_inputs_bundles_resume_and_prompt() -> None:
    """PromptInputs is a small value object the stages consume."""
    inputs = PromptInputs(resume="CV", prompt="x")
    assert inputs.resume == "CV"
    assert inputs.prompt == "x"
