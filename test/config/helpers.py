"""Shared helpers for the pipeline-config test suites.

Write a valid mock ``pipeline.yaml`` plus all referenced files (credentials,
api key, resume, prompt templates) into an isolated runtime config dir, and
return the path. The referenced-file paths are absolute, so they are
location-independent.
"""

from __future__ import annotations

from pathlib import Path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def build_valid_pipeline_yaml(
    config_dir: Path,
    *,
    daily_apply_limit: int = 5,
    behavior_yaml: str = "",
    vacancies_yaml: str = "",
) -> Path:
    """Write a valid mock pipeline.yaml + referenced files; return its path.

    Uses absolute paths under ``config_dir`` so the fixture does not depend on
    the repo layout.

    ``behavior_yaml`` and ``vacancies_yaml`` are optional, already-indented
    blocks (the 8-space ``vacancies:`` key matching the ``filter`` key inside
    the search entry, and the 4-space ``behavior:`` key matching the section
    level) injected into the ``service.mock`` section right after the ``filter``
    block, so tests can parse a ``behavior:``/``vacancies:`` block. An empty
    value keeps the output byte-identical to the default.
    """
    login_file = config_dir / "secrets" / "login.txt"
    password_file = config_dir / "secrets" / "password.txt"
    api_key_file = config_dir / "secrets" / "api_key.txt"
    resume_file = config_dir / "resume.md"
    score_prompt = config_dir / "prompts" / "score.md.j2"
    apply_prompt = config_dir / "prompts" / "apply.md.j2"

    # The trailing newlines are deliberate: real referenced files end with one
    # (editor/echo artifact), and the loader must strip it at load time — the
    # config suites assert the loaded values WITHOUT the newline.
    _write(login_file, "mock-login\n")
    _write(password_file, "mock-password\n")
    _write(api_key_file, "mock-api-key\n")
    _write(resume_file, "# Mock CV\n")
    _write(score_prompt, "score template\n")
    _write(apply_prompt, "apply template\n")

    behavior_block = "" if not behavior_yaml else "\n" + behavior_yaml
    vacancies_block = "" if not vacancies_yaml else "\n" + vacancies_yaml

    # as_posix: Windows backslashes are invalid escapes inside YAML
    # double-quoted scalars; forward slashes resolve fine everywhere.
    yaml_text = f"""\
name: "mock-demo"
description: "A config test pipeline"
service:
  mock:
    resume_id: "mock-resume-1"
    searches:
      - query: "python"
        filter:
          area: [1]
          schedule: ["fullDay", "remote"]
          experience: "between1And3"
          only_with_salary: true
{vacancies_block}{behavior_block}auth:
  login_file: "{login_file.as_posix()}"
  password_file: "{password_file.as_posix()}"
resume:
  path: "{resume_file.as_posix()}"
openai:
  model: "gpt-5-mini"
  base_url: "https://api.openai.com/v1"
  api_key_file: "{api_key_file.as_posix()}"
scoring:
  min_required_score: 3
  scoring_prompt_file: "{score_prompt.as_posix()}"
apply:
  apply_prompt_file: "{apply_prompt.as_posix()}"
limits:
  daily_apply_limit: {daily_apply_limit}
"""
    config_path = config_dir / "pipeline.yaml"
    _write(config_path, yaml_text)
    return config_path


def write_pipeline_files(
    runtime_dir: Path,
    *,
    daily_apply_limit: int = 5,
    behavior_yaml: str = "",
) -> Path:
    """Write the pipeline files into ``runtime_dir/config`` (the fixture layout)."""
    config_dir = runtime_dir / "config"
    return build_valid_pipeline_yaml(
        config_dir,
        daily_apply_limit=daily_apply_limit,
        behavior_yaml=behavior_yaml,
    )


def build_inline_pipeline_yaml(config_dir: Path) -> Path:
    """Write a pipeline.yaml that specifies EVERY content slot inline (no files).

    No referenced file is read while loading — every content value (login,
    password, resume, api_key, scoring/apply prompt) is given inline.
    """
    yaml_text = """\
name: "inline-demo"
description: "A config test pipeline with inline contents"
service:
  mock:
    resume_id: "mock-resume-1"
    searches:
      - query: "python"
        filter:
          area: [1]
          schedule: ["fullDay", "remote"]
          experience: "between1And3"
          only_with_salary: false
auth:
  login: "inline-login"
  password: "inline-password"
resume:
  contents: "# Inline CV\\n"
openai:
  model: "gpt-5-mini"
  base_url: "https://api.openai.com/v1"
  api_key: "inline-api-key"
scoring:
  min_required_score: 3
  scoring_prompt: "inline score template"
apply:
  apply_prompt: "inline apply template"
limits:
  daily_apply_limit: 5
"""
    config_path = config_dir / "pipeline.yaml"
    _write(config_path, yaml_text)
    return config_path
