"""HH test solver selection (mirrors ``captcha/selector.py`` for tests).

Core-owned selection chain per the feature decision (D5/D8): the explicit
answers file wins, then the configured AI path (the ``hh_test_solving`` config
section's prompt + the main ``openai`` section), unless ``--no-test-ai`` forces
the file-only path. Returns ``None`` when no solver is available — NOT a hard
failure: the apply stage turns "test present but no solver" into a per-vacancy
failure, so a pipeline without the section or file still applies everything
else.
"""

from __future__ import annotations

from pathlib import Path

from jobfucker.config import PipelineConfig
from jobfucker.hh_tests.ai import HhTestAiSolver
from jobfucker.hh_tests.contract import HhTestSolver
from jobfucker.hh_tests.file import HhTestFileSolver

__all__ = ["select_hh_test_solver"]


def _prompt_parses(text: str) -> bool:
    """Whether the configured template text parses (frontmatter + Jinja2 body).

    Lazily imported: ``stages/__init__`` pulls the whole domain (ai → config),
    which would close a load-time cycle via ``config``; the same lazy pattern
    ``config.validate_search_windows`` uses.
    """
    from jobfucker.stages.prompts import parse_prompt_template

    return parse_prompt_template(text).is_ok


def select_hh_test_solver(
    config: PipelineConfig,
    *,
    answers_file: Path | None = None,
    no_test_ai: bool = False,
) -> HhTestSolver | None:
    """Select the hh test solver for a run: explicit file, else configured AI.

    Args:
        config: the validated pipeline config (its ``hh_test_solving`` section
            drives the AI branch; the main ``openai`` section powers it).
        answers_file: an explicit ``--test-answers`` file; it wins over AI.
        no_test_ai: the ``--no-test-ai`` CLI toggle, mirroring ``--no-captcha-ai``.

    Returns:
        The selected solver, or ``None`` when neither a file nor a usable AI
        config/prompt is available.
    """
    if answers_file is not None:
        return HhTestFileSolver(answers_file)

    if no_test_ai:
        return None

    section = config.hh_test_solving
    if section is None or not section.enabled:
        return None
    if not section.test_prompt.strip():
        return None
    if not _prompt_parses(section.test_prompt):
        # An invalid prompt template fails the AI branch quietly: the stage
        # surfaces the per-vacancy "no solver" failure instead of boot-crashing
        # a run that may only need answers from a file.
        return None
    return HhTestAiSolver(
        config.openai,
        allow_unsolved=section.allow_ai_to_skip_test_when_not_enough_context,
    )
