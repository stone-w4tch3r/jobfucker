"""BDD acceptance for the hh screening-test solving module (test/AGENTS.md §3b)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest
from pytest_bdd import given, scenarios, then, when
from rusty_results.prelude import Result

from jobfucker.ai import ChatRequest
from jobfucker.clients.base import ServiceVacancyId
from jobfucker.config import (
    ApplyConfig,
    AuthConfig,
    HhTestSolvingConfig,
    LimitsConfig,
    OpenAIConfig,
    PipelineConfig,
    ResumeConfig,
    ScoringConfig,
)
from jobfucker.hh_tests.ai import HhTestAiSolver
from jobfucker.hh_tests.contract import (
    HhTestOption,
    HhTestProblem,
    HhTestSolution,
    HhTestSolutionDocument,
    HhTestSolveOutcome,
    HhTestSolver,
    HhTestTask,
    HhTestUnsolved,
    validate_hh_test_solution,
)
from jobfucker.hh_tests.dump import HhTestDumpDocument, HhTestDumpRecord, dump_problems_to_json
from jobfucker.hh_tests.file import HhTestFileSolver
from jobfucker.hh_tests.prompt import render_hh_test_prompt
from jobfucker.hh_tests.selector import select_hh_test_solver
from jobfucker.stages.prompts import PromptTemplate, VacancyPromptData
from jobfucker.testing.step_runner import async_run

scenarios("bdd/hh_tests.feature")

_VACANCY_ID = "111570490"
_DECLINE_COMMENT = "resume does not mention relocation"


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Zero the shared retry backoff so deterministic failures stay instant."""
    monkeypatch.setattr("jobfucker.ai._BACKOFF_BASE_SECONDS", 0.0)


@dataclass(frozen=True, slots=True)
class ProblemCase:
    """The fixed screening test plus the document under test."""

    problem: HhTestProblem
    document: HhTestSolutionDocument


@dataclass(frozen=True, slots=True)
class RecordingCompletion:
    """A scripted AI completion that records how many times it ran."""

    payload: str
    prompts: list[str] = field(default_factory=list[str])

    async def __call__(self, request: ChatRequest) -> str:
        self.prompts.append(str(request.messages[0].content))
        return self.payload


@dataclass(frozen=True, slots=True)
class ScriptedCompletion:
    """A completion returning each payload in order (deterministic retry script)."""

    payloads: tuple[str, ...]
    prompts: list[str] = field(default_factory=list[str])

    async def __call__(self, request: ChatRequest) -> str:
        index = min(len(self.prompts), len(self.payloads) - 1)
        self.prompts.append(str(request.messages[0].content))
        return self.payloads[index]


@dataclass(frozen=True, slots=True)
class AiCase:
    """The AI solver plus its recording completion."""

    solver: HhTestAiSolver
    completion: RecordingCompletion | ScriptedCompletion


@dataclass(frozen=True, slots=True)
class SelectorCase:
    """The config and optional answers-file argument for selection."""

    config: PipelineConfig
    answers_file: Path | None


def _problem() -> HhTestProblem:
    """The fixed two-task test: one free text, one single choice."""
    return HhTestProblem(
        vacancy_id=ServiceVacancyId(_VACANCY_ID),
        name="Запрос СТДР",
        description="Ответьте на вопросы",
        tasks=(
            HhTestTask(id="1", kind="free_text", prompt="Почему вы?"),
            HhTestTask(
                id="2",
                kind="choice",
                prompt="Готовы к переезду?",
                options=(HhTestOption(id="20", label="Да"), HhTestOption(id="21", label="Нет")),
            ),
        ),
    )


def _document(*, task_one_text: str = "Потому что", option_id: str = "20") -> HhTestSolutionDocument:
    """A complete answer document with overridable per-task answers."""
    return HhTestSolutionDocument.model_validate({"1": {"text": task_one_text}, "2": {"option_id": option_id}})


def _config(*, hh_test_solving: HhTestSolvingConfig | None) -> PipelineConfig:
    """A minimal validated config carrying the optional hh test-solving section."""
    return PipelineConfig(
        name="hh-tests-bdd",
        description="",
        service="mock",
        auth=AuthConfig(login="login", password="password"),
        resume=ResumeConfig(contents="resume"),
        openai=OpenAIConfig(model="gpt-test", base_url="https://api.example.com/v1", api_key="key"),
        scoring=ScoringConfig(min_required_score=3, scoring_prompt="Score {{ vacancy_formatted }}"),
        apply=ApplyConfig(apply_prompt="Apply {{ vacancy_formatted }}"),
        limits=LimitsConfig(daily_apply_limit=50),
        hh_test_solving=hh_test_solving,
    )


# --- Given: the problem under test -------------------------------------------
@given("a screening test with a free-text and a choice task", target_fixture="problem_case")
def problem_step() -> ProblemCase:
    return ProblemCase(problem=_problem(), document=_document())


@given(
    "a screening test with a free-text and a choice task missing the choice answer",
    target_fixture="problem_case",
)
def problem_missing_answer_step() -> ProblemCase:
    return ProblemCase(
        problem=_problem(), document=HhTestSolutionDocument.model_validate({"1": {"text": "Потому что"}})
    )


@given(
    "a screening test with a free-text and a choice task choosing an unknown option",
    target_fixture="problem_case",
)
def problem_unknown_option_step() -> ProblemCase:
    return ProblemCase(problem=_problem(), document=_document(option_id="999"))


# --- Given: file solver ------------------------------------------------------
@given("an answers file containing a valid document for that vacancy", target_fixture="file_solver")
def file_with_answers_step(tmp_path: Path) -> HhTestFileSolver:
    path = tmp_path / "answers.json"
    path.write_text(
        json.dumps({_VACANCY_ID: {"1": {"text": "Потому что"}, "2": {"option_id": "20"}}}), encoding="utf-8"
    )
    return HhTestFileSolver(path)


@given("an answers file without that vacancy", target_fixture="file_solver")
def file_without_answers_step(tmp_path: Path) -> HhTestFileSolver:
    path = tmp_path / "answers.json"
    path.write_text(json.dumps({"999": {"1": {"text": "x"}}}), encoding="utf-8")
    return HhTestFileSolver(path)


@given(
    "an answers file containing a valid document for that vacancy referenced through ~",
    target_fixture="file_solver",
)
def file_with_answers_via_tilde_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> HhTestFileSolver:
    path = tmp_path / "answers.json"
    path.write_text(
        json.dumps({_VACANCY_ID: {"1": {"text": "Потому что"}, "2": {"option_id": "20"}}}), encoding="utf-8"
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    return HhTestFileSolver(Path("~/answers.json"))


# --- Given: AI solver --------------------------------------------------------
def _ai_case(payload: str, *, allow_unsolved: bool) -> AiCase:
    """Build the solver + its recording completion for a scripted payload."""
    completion = RecordingCompletion(payload=payload)
    return AiCase(
        solver=HhTestAiSolver(
            OpenAIConfig(model="gpt-test", base_url="https://api.example.com/v1", api_key="key"),
            allow_unsolved=allow_unsolved,
            completion=completion,
        ),
        completion=completion,
    )


def _answer_payload() -> str:
    return json.dumps({"1": {"text": "Потому что"}, "2": {"option_id": "20"}})


def _decline_payload() -> str:
    return json.dumps({"not_enough_context_for_test": _DECLINE_COMMENT})


@given("an AI completion returning a complete answer document", target_fixture="ai_case")
def ai_complete_step() -> AiCase:
    return _ai_case(_answer_payload(), allow_unsolved=False)


@given("an AI completion returning an invalid answer document", target_fixture="ai_case")
def ai_invalid_step() -> AiCase:
    return _ai_case(json.dumps({"1": {"answer": "not a known field"}}), allow_unsolved=False)


@given("an AI completion returning a decline document", target_fixture="ai_case")
def ai_decline_step() -> AiCase:
    return _ai_case(_decline_payload(), allow_unsolved=False)


@given("an AI completion returning answers with the fallback enabled", target_fixture="ai_case")
def ai_complete_fallback_step() -> AiCase:
    return _ai_case(_answer_payload(), allow_unsolved=True)


@given("an AI completion returning a decline document with the fallback enabled", target_fixture="ai_case")
def ai_decline_fallback_step() -> AiCase:
    return _ai_case(_decline_payload(), allow_unsolved=True)


@given("an AI completion returning an invalid then a complete answer document", target_fixture="ai_case")
def ai_retry_step() -> AiCase:
    completion = ScriptedCompletion(payloads=(json.dumps({"1": {"answer": "bad"}}), _answer_payload()))
    return AiCase(
        solver=HhTestAiSolver(
            OpenAIConfig(model="gpt-test", base_url="https://api.example.com/v1", api_key="key"),
            completion=completion,
        ),
        completion=completion,
    )


@given("an AI completion returning a decline document is the default completion")
def default_decline_step(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the solver's default completion so a selected AI solver declines."""
    completion = RecordingCompletion(payload=_decline_payload())
    monkeypatch.setattr(
        "jobfucker.hh_tests.ai.default_completions",
        lambda _config: completion,  # type: ignore[reportUnknownLambdaType]  # rationale: str-target setattr bypasses the typed overload; the lambda mirrors default_completions(config)
    )


# --- Given: selector ---------------------------------------------------------
@given("an hh test-solving config with a prompt and an openai section", target_fixture="selector_case")
def selector_config_step() -> SelectorCase:
    section = HhTestSolvingConfig(enabled=True, test_prompt="Solve: {{ test_formatted }}")
    return SelectorCase(config=_config(hh_test_solving=section), answers_file=None)


@given("an hh test-solving config with the AI decline fallback enabled", target_fixture="selector_case")
def selector_config_fallback_step() -> SelectorCase:
    section = HhTestSolvingConfig(
        enabled=True,
        allow_ai_to_skip_test_when_not_enough_context=True,
        test_prompt="Solve: {{ test_formatted }}",
    )
    return SelectorCase(config=_config(hh_test_solving=section), answers_file=None)


@given("a test-answers file argument", target_fixture="selector_case")
def selector_answers_file_step(selector_case: SelectorCase, tmp_path: Path) -> SelectorCase:
    return replace(selector_case, answers_file=tmp_path / "answers.json")


@given("no hh test-solving config", target_fixture="selector_case")
def selector_no_config_step() -> SelectorCase:
    return SelectorCase(config=_config(hh_test_solving=None), answers_file=None)


@given("an hh test-solving config with AI disabled", target_fixture="selector_case")
def selector_disabled_step() -> SelectorCase:
    section = HhTestSolvingConfig(enabled=False, test_prompt="Solve: {{ test_formatted }}")
    return SelectorCase(config=_config(hh_test_solving=section), answers_file=None)


@given("an hh test-solving config with an invalid prompt template", target_fixture="selector_case")
def selector_invalid_prompt_step() -> SelectorCase:
    section = HhTestSolvingConfig(enabled=True, test_prompt="---\nbad: [unclosed\n---\nSolve")
    return SelectorCase(config=_config(hh_test_solving=section), answers_file=None)


@given("a screening test whose free-text task carries an option", target_fixture="problem_case")
def problem_free_text_option_step() -> ProblemCase:
    document = HhTestSolutionDocument.model_validate({"1": {"option_id": "20"}, "2": {"option_id": "20"}})
    return ProblemCase(problem=_problem(), document=document)


@given("a screening test with an extra unanswered task", target_fixture="problem_case")
def problem_extra_task_step() -> ProblemCase:
    base = _problem()
    problem = replace(base, tasks=(*base.tasks, HhTestTask(id="3", kind="free_text", prompt="Ещё вопрос?")))
    return ProblemCase(problem=problem, document=_document())


@given("an hh test prompt template injecting resume, vacancy and test", target_fixture="prompt_template")
def prompt_template_step() -> PromptTemplate:
    return PromptTemplate(
        params={},
        body="Resume {{ resume_formatted }} | Vacancy {{ vacancy_formatted }} | Test {{ test_formatted }}",
    )


# --- When --------------------------------------------------------------------
@when("the solution is validated", target_fixture="outcome")
def validate_step(problem_case: ProblemCase) -> Result[HhTestSolution, str]:
    return validate_hh_test_solution(problem_case.problem, problem_case.document)


@when("the file solver solves the test", target_fixture="outcome")
def file_solve_step(problem_case: ProblemCase, file_solver: HhTestFileSolver) -> Result[HhTestSolveOutcome, str]:
    return async_run(file_solver(problem_case.problem, ""))


@when("the AI solver solves the test", target_fixture="outcome")
def ai_solve_step(problem_case: ProblemCase, ai_case: AiCase) -> Result[HhTestSolveOutcome, str]:
    return async_run(ai_case.solver(problem_case.problem, "rendered prompt"))


@when("the hh test solver is selected", target_fixture="selected_solver")
def select_step(selector_case: SelectorCase) -> HhTestSolver | None:
    return select_hh_test_solver(selector_case.config, answers_file=selector_case.answers_file)


@when("the hh test solver is selected with AI disabled", target_fixture="selected_solver")
def select_no_ai_step(selector_case: SelectorCase) -> HhTestSolver | None:
    return select_hh_test_solver(selector_case.config, answers_file=selector_case.answers_file, no_test_ai=True)


@when("the selected solver solves the test", target_fixture="outcome")
def selected_solver_solves_step(
    problem_case: ProblemCase,
    selected_solver: HhTestSolver | None,
) -> Result[HhTestSolveOutcome, str]:
    assert selected_solver is not None
    return async_run(selected_solver(problem_case.problem, "rendered prompt"))


@when("the hh test prompt is rendered", target_fixture="rendered_prompt")
def render_prompt_step(
    problem_case: ProblemCase,
    prompt_template: PromptTemplate,
) -> Result[str, str]:
    return render_hh_test_prompt(
        prompt_template,
        resume="my resume text",
        vacancy=VacancyPromptData(title="Python разработчик", company="Компания", description="Описание", salary=None),
        test=problem_case.problem,
    )


@when("the test is dumped", target_fixture="dump_json")
def dump_step(problem_case: ProblemCase) -> str:
    record = HhTestDumpRecord(problem=problem_case.problem, title="Python разработчик", url="https://hh.ru/vacancy/1")
    return dump_problems_to_json((record,))


# --- Then --------------------------------------------------------------------
@then("the solution carries both task answers")
def assert_both_answers(outcome: Result[HhTestSolution, str]) -> None:
    assert outcome.is_ok
    solution = outcome.unwrap()
    assert solution["1"].text == "Потому что"
    assert solution["2"].option_id == "20"


@then("validation fails mentioning the unanswered task")
def assert_missing_task(outcome: Result[HhTestSolution, str]) -> None:
    assert outcome.is_err
    assert "unanswered task" in outcome.unwrap_err()


@then("validation fails")
def assert_validation_fails(outcome: Result[HhTestSolution, str]) -> None:
    assert outcome.is_err


@then("the solve returns both task answers")
def assert_solve_answers(outcome: Result[HhTestSolveOutcome, str]) -> None:
    assert outcome.is_ok
    value = outcome.unwrap()
    assert isinstance(value, dict)
    assert set(value) == {"1", "2"}


@then("the rendered prompt carries the resume, the vacancy and both task prompts")
def assert_rendered_prompt(rendered_prompt: Result[str, str]) -> None:
    assert rendered_prompt.is_ok
    text = rendered_prompt.unwrap()
    assert "my resume text" in text
    assert "Python разработчик" in text
    assert "Task 1 (free text): Почему вы?" in text
    assert "Task 2 (choice): Готовы к переезду?" in text
    assert "(20) Да" in text


@then("the solve fails mentioning the vacancy")
def assert_solve_vacancy_fails(outcome: Result[HhTestSolveOutcome, str]) -> None:
    assert outcome.is_err
    assert _VACANCY_ID in outcome.unwrap_err()


@then("the solve fails")
def assert_solve_fails(outcome: Result[HhTestSolveOutcome, str]) -> None:
    assert outcome.is_err


@then("the solve is declined with the comment")
def assert_solve_declined(outcome: Result[HhTestSolveOutcome, str]) -> None:
    assert outcome.is_ok
    value = outcome.unwrap()
    assert isinstance(value, HhTestUnsolved)
    assert value.comment == _DECLINE_COMMENT


@then("the AI completion ran once")
def assert_completion_once(ai_case: AiCase) -> None:
    assert len(ai_case.completion.prompts) == 1
    # The user message carries the code-owned instruction + the rendered context.
    assert ai_case.completion.prompts[0].endswith("rendered prompt")


@then("the AI completion ran twice")
def assert_completion_twice(ai_case: AiCase) -> None:
    assert len(ai_case.completion.prompts) == 2


@then("the prompt offers the decline fallback")
def assert_prompt_offers_fallback(ai_case: AiCase) -> None:
    assert "not_enough_context_for_test" in ai_case.completion.prompts[0]


@then("the prompt offers no decline fallback")
def assert_prompt_offers_no_fallback(ai_case: AiCase) -> None:
    assert "not_enough_context_for_test" not in ai_case.completion.prompts[0]


@then("the file solver is selected")
def assert_file_selected(selected_solver: HhTestSolver | None) -> None:
    assert isinstance(selected_solver, HhTestFileSolver)


@then("the AI solver is selected")
def assert_ai_selected(selected_solver: HhTestSolver | None) -> None:
    assert isinstance(selected_solver, HhTestAiSolver)


@then("no solver is selected")
def assert_no_solver(selected_solver: HhTestSolver | None) -> None:
    assert selected_solver is None


@then("the dump document carries the test name, the vacancy meta and both task ids")
def assert_dump_document(dump_json: str) -> None:
    entry = HhTestDumpDocument.model_validate_json(dump_json).root[_VACANCY_ID]
    assert entry.name == "Запрос СТДР"
    assert entry.meta.title == "Python разработчик"
    assert [task.id for task in entry.tasks] == ["1", "2"]
    assert entry.tasks[1].options[0].id == "20"
