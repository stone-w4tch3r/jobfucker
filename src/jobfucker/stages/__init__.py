"""Engine stages (Phase 5).

Each stage is a callable, typer-free function the engine controller composes:
fetch → score → generate_cv → apply. Phase 5A ships the AI-heavy stages:

- :mod:`jobfucker.stages.prompts` — prompt loading + Jinja2 rendering;
- :mod:`jobfucker.stages.score` — AI scoring (1..5); sub-threshold is derived
  from ``score`` vs the snapshot threshold;
- :mod:`jobfucker.stages.generate_cv` — cover-letter generation for eligible
  vacancies;
- :mod:`jobfucker.stages.fetch` — paginate + persist fetched vacancies (Phase 5B);
- :mod:`jobfucker.stages.apply` — apply with dual-limit enforcement (Phase 5B).
"""

from __future__ import annotations

from jobfucker.stages.apply import AppliedVacancy, ApplyReport, ApplyTargets, run_apply
from jobfucker.stages.fetch import FetchedVacancy, FetchInputs, FetchReport, run_fetch
from jobfucker.stages.generate_cv import GenerateCvReport, GeneratedCv, run_generate_cv
from jobfucker.stages.prompts import (
    VacancyPromptData,
    render_apply_prompt,
    render_prompt,
    render_scoring_prompt,
)
from jobfucker.stages.score import ScoredVacancy, ScoreReport, run_score

__all__ = [
    "AppliedVacancy",
    "ApplyReport",
    "ApplyTargets",
    "FetchInputs",
    "FetchReport",
    "FetchedVacancy",
    "GenerateCvReport",
    "GeneratedCv",
    "ScoreReport",
    "ScoredVacancy",
    "VacancyPromptData",
    "render_apply_prompt",
    "render_prompt",
    "render_scoring_prompt",
    "run_apply",
    "run_fetch",
    "run_generate_cv",
    "run_score",
]
