"""Doctor checks: verify the real configured setup with zero stored-data writes.

Each check is a small, UI-neutral coroutine the CLI orchestrates; they are
kept here (not in ``cli.py``) so tests can drive them with stub clients,
handlers, and AI without a TTY or network:

- :func:`check_identity` — a board "whoami" through :meth:`Client.get_identity`
  (HH: the mandatory ``/me`` healthcheck, no extra request);
- :func:`check_captcha` — feeds the packaged mock captcha PNG through the
  *selected* handler (terminal sixel/kitty or AI vision) and requires a
  non-empty answer, proving the configured captcha path works end-to-end;
- :func:`check_scoring` — renders the pipeline's real scoring prompt over a
  canned resource vacancy (:data:`DOCTOR_VACANCY_PATH`) and runs one throwaway
  ``ai.score`` (nothing is persisted), proving the OpenAI setup works.

The resource loaders validate at the boundary (pydantic) and return ``Result``
— a missing/corrupt packaged resource is an expected failure, not a crash.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.ai import AiClient, ScoreResult
from jobfucker.clients.base import CaptchaHandler, Client, ServiceIdentity
from jobfucker.config import parse_yaml_object
from jobfucker.stages.prompts import VacancyPromptData, render_scoring_prompt

__all__ = [
    "CAPTCHA_IMAGE_PATH",
    "DOCTOR_VACANCY_PATH",
    "DoctorVacancy",
    "check_captcha",
    "check_identity",
    "check_scoring",
    "load_captcha_image",
    "load_doctor_vacancy",
]

_RESOURCES_DIR = Path(__file__).parent / "resources"
DOCTOR_VACANCY_PATH = _RESOURCES_DIR / "doctor_vacancy.yaml"
CAPTCHA_IMAGE_PATH = _RESOURCES_DIR / "mock_client_captcha.png"


class DoctorVacancy(BaseModel):
    """The canned resource vacancy the doctor scores (a real-shaped fake).

    ``salary`` is a display string — the doctor only feeds the rendered
    prompt, it never builds a contract :class:`~jobfucker.clients.base.Vacancy`.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    company: str | None = None
    description: str
    salary: str | None = None


def load_doctor_vacancy(path: Path = DOCTOR_VACANCY_PATH) -> Result[DoctorVacancy, str]:
    """Load + validate the canned resource vacancy."""
    try:
        raw = parse_yaml_object(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return Err(f"Cannot read doctor vacancy resource {path}: {exc}")
    try:
        return Ok(DoctorVacancy.model_validate(raw))
    except ValidationError as exc:
        return Err(f"Invalid doctor vacancy resource {path}: {exc}")


def load_captcha_image(path: Path = CAPTCHA_IMAGE_PATH) -> Result[bytes, str]:
    """Load the packaged mock captcha PNG used to trigger the real handler."""
    try:
        return Ok(path.read_bytes())
    except OSError as exc:
        return Err(f"Cannot read captcha image resource {path}: {exc}")


async def check_identity(client: Client) -> Result[ServiceIdentity, str]:
    """Fetch the authenticated board-account identity ("whoami")."""
    result = await client.get_identity()
    if result.is_err:
        return Err(result.unwrap_err().message)
    return Ok(result.unwrap())


async def check_captcha(handler: CaptchaHandler, image: bytes) -> Result[str, str]:
    """Solve one captcha through the real handler (terminal or AI vision)."""
    solved = await handler(image)
    if solved.is_err:
        return Err(solved.unwrap_err())
    return Ok(solved.unwrap())


async def check_scoring(
    ai: AiClient,
    *,
    resume: str,
    prompt: str,
    vacancy: DoctorVacancy,
) -> Result[ScoreResult, str]:
    """Run one throwaway AI scoring of the canned vacancy (nothing persisted)."""
    rendered = render_scoring_prompt(
        prompt,
        resume=resume,
        vacancy=VacancyPromptData(
            title=vacancy.title,
            company=vacancy.company,
            description=vacancy.description,
            salary=vacancy.salary,
        ),
    )
    if rendered.is_err:
        return Err(rendered.unwrap_err())
    result = await ai.score(rendered.unwrap())
    if result.is_err:
        return Err(result.unwrap_err().message)
    return Ok(result.unwrap())
