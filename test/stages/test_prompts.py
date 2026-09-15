"""Phase 5A (task 5.3): unit tests for prompt frontmatter parsing + rendering.

Pure unit tests (plain pytest): parse ``---``-delimited YAML frontmatter off a
Jinja2 body, load from a file, and render the body with injected resume /
vacancy / score / reasoning content.
"""

from __future__ import annotations

from pathlib import Path

from jobfucker.stages.prompts import (
    PromptInputs,
    PromptTemplate,
    VacancyPromptData,
    load_prompt_template,
    parse_prompt_template,
    render_apply_prompt,
    render_prompt,
    render_scoring_prompt,
)

SCORE_TEMPLATE = """---
purpose: resume-vacancy matching
scale: 1-5
---
Rate the match between this resume and vacancy.

Resume:
{{ resume_formatted }}

Vacancy: {{ vacancy_formatted }}
"""

APPLY_TEMPLATE = """---
purpose: cover letter
---
Write a cover letter using the resume and vacancy.

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


# --- frontmatter parsing --------------------------------------------------------
def test_parse_frontmatter_params_and_body() -> None:
    """YAML frontmatter becomes params; the rest is the Jinja2 body."""
    result = parse_prompt_template(SCORE_TEMPLATE)
    assert result.is_ok
    template = result.unwrap()
    assert template.params.get("purpose") == "resume-vacancy matching"
    assert template.params.get("scale") == "1-5"
    assert "Rate the match" in template.body
    assert "{{ resume_formatted }}" in template.body


def test_parse_template_without_frontmatter() -> None:
    """A body with no frontmatter yields empty params and the whole text as body."""
    result = parse_prompt_template("Just a {{ body }}")
    assert result.is_ok
    template = result.unwrap()
    assert template.params == {}
    assert template.body == "Just a {{ body }}"


def test_parse_unclosed_frontmatter_is_err() -> None:
    """An opening '---' without a closing '---' is a clear Err."""
    result = parse_prompt_template("---\nfoo: bar\nno closing delimiter")
    assert result.is_err
    assert "closing" in result.unwrap_err()


def test_parse_malformed_frontmatter_yaml_is_err() -> None:
    """Invalid YAML in the frontmatter is a clear Err."""
    result = parse_prompt_template("---\n: : :\n---\nbody")
    assert result.is_err
    assert "frontmatter" in result.unwrap_err()


def test_load_prompt_template_from_file(tmp_path: Path) -> None:
    """load_prompt_template reads + parses a template file."""
    path = tmp_path / "score.md.j2"
    path.write_text(SCORE_TEMPLATE, encoding="utf-8")
    result = load_prompt_template(path)
    assert result.is_ok
    assert result.unwrap().params.get("purpose") == "resume-vacancy matching"


def test_load_prompt_template_missing_file_is_err(tmp_path: Path) -> None:
    """A missing template file is a clear Err."""
    result = load_prompt_template(tmp_path / "nope.md.j2")
    assert result.is_err
    assert "Could not read prompt template" in result.unwrap_err()


# --- rendering ------------------------------------------------------------------
def test_render_scoring_prompt_injects_resume_and_vacancy() -> None:
    """The scoring body receives resume_formatted + vacancy_formatted."""
    template = parse_prompt_template(SCORE_TEMPLATE).unwrap()
    rendered = render_scoring_prompt(template, resume="My CV", vacancy=_vacancy())
    assert rendered.is_ok
    text = rendered.unwrap()
    assert "My CV" in text
    assert "Senior Backend" in text
    assert "ACME" in text
    assert "Build FastAPI services." in text


def test_render_apply_prompt_injects_resume_and_vacancy() -> None:
    """The apply body receives resume_formatted + vacancy_formatted (no score/reasoning)."""
    template = parse_prompt_template(APPLY_TEMPLATE).unwrap()
    rendered = render_apply_prompt(
        template,
        resume="My CV",
        vacancy=_vacancy(),
    )
    assert rendered.is_ok
    text = rendered.unwrap()
    assert "My CV" in text
    assert "Senior Backend" in text


def test_render_prompt_with_empty_company_is_ok() -> None:
    """A vacancy with no company renders without a company line, not an error."""
    template = parse_prompt_template(SCORE_TEMPLATE).unwrap()
    vacancy = VacancyPromptData(title="X", company=None, description="", salary=None)
    rendered = render_scoring_prompt(template, resume="CV", vacancy=vacancy)
    assert rendered.is_ok
    assert "Title: X" in rendered.unwrap()


def test_render_missing_variable_is_err() -> None:
    """A template referencing a variable not in the context fails fast."""
    template = parse_prompt_template("Score {{ resume_formatted }} and {{ unknown_var }}").unwrap()
    rendered = render_scoring_prompt(template, resume="CV", vacancy=_vacancy())
    assert rendered.is_err
    assert "render failed" in rendered.unwrap_err().lower()


def test_render_invalid_jinja_is_err() -> None:
    """A broken Jinja2 body is a clear Err."""
    template = parse_prompt_template("Unclosed: {{ resume_formatted").unwrap()
    rendered = render_prompt(template, context={"resume_formatted": "x"})
    assert rendered.is_err
    assert "render failed" in rendered.unwrap_err().lower()


def test_prompt_inputs_bundles_resume_and_template() -> None:
    """PromptInputs is a small value object the stages consume."""
    template = PromptTemplate(params={}, body="x")
    inputs = PromptInputs(resume="CV", prompt=template)
    assert inputs.resume == "CV"
    assert inputs.prompt is template
