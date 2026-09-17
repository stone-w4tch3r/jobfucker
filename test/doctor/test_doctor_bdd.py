"""BDD acceptance coverage for the doctor checks (identity / captcha / scoring).

State flows by fixture injection (test/AGENTS.md §3b): each ``Given`` seeds one
setup piece (a ``Client`` double, a captcha handler stub, a scripted AI), the
``When`` returns the raw ``Result`` of one doctor check, and the ``Then`` steps
only assert. The async checks run through the shared step runner.
"""

from __future__ import annotations

from pathlib import Path
from typing import override

from pytest_bdd import given, scenarios, then, when
from rusty_results.prelude import Err, Ok, Result

from jobfucker.ai import ScoreError, ScoreResult
from jobfucker.clients.base import (
    AuthError,
    CaptchaHandler,
    Client,
    ClientDeps,
    ClientError,
    ServiceIdentity,
)
from jobfucker.doctor import (
    DoctorVacancy,
    check_captcha,
    check_identity,
    check_scoring,
    load_captcha_image,
    load_doctor_vacancy,
)
from jobfucker.testing.step_runner import async_run
from test.fakes.fake_client import FakeClient, FakeServiceConfig
from test.pipeline_helpers import ScriptedAi

scenarios("bdd/doctor.feature")


class _FailingIdentityFake(FakeClient):
    """A fake whose identity fetch fails like a rejected board account."""

    @override
    async def get_identity(self) -> Result[ServiceIdentity, ClientError]:
        return Err(AuthError(message="board rejected the account"))


def _ok_handler(answer: str) -> CaptchaHandler:
    """A captcha handler stub that always solves."""

    async def handler(image: bytes) -> Result[str, str]:
        del image
        return Ok(answer)

    return handler


def _err_handler(message: str) -> CaptchaHandler:
    """A captcha handler stub that never solves."""

    async def handler(image: bytes) -> Result[str, str]:
        del image
        return Err(message)

    return handler


# --- Givens (seed one setup piece each) ---------------------------------------


@given("a fake client with a working identity", target_fixture="client")
def working_identity_client_step(client_deps: ClientDeps) -> FakeClient:
    """The all-succeed fake: ``get_identity`` returns the canned identity."""
    return FakeClient(client_deps, FakeServiceConfig(resume_id="fake-resume-1"))  # type: ignore[arg-type]  # rationale: unregistered dataclass double, deliberately outside the protocol (same as the conformance test)


@given("a fake client whose identity fetch fails", target_fixture="client")
def failing_identity_client_step(client_deps: ClientDeps) -> _FailingIdentityFake:
    """The scripted-rejection fake for the failure path."""
    return _FailingIdentityFake(client_deps, FakeServiceConfig(resume_id="fake-resume-1"))  # type: ignore[arg-type]  # rationale: unregistered dataclass double, deliberately outside the protocol (same as the conformance test)


@given("a captcha handler that solves", target_fixture="captcha_handler")
def solving_handler_step() -> CaptchaHandler:
    return _ok_handler("1234")


@given("a captcha handler that cannot solve", target_fixture="captcha_handler")
def unsolving_handler_step() -> CaptchaHandler:
    return _err_handler("captcha input ended (EOF)")


@given("a scripted AI that scores 4", target_fixture="scripted_ai")
def scoring_ai_step() -> ScriptedAi:
    return ScriptedAi(scores=[Ok(ScoreResult(fit_score=4, comment="good match"))])


@given("a scripted AI whose score fails", target_fixture="scripted_ai")
def failing_ai_step() -> ScriptedAi:
    return ScriptedAi(scores=[Err(ScoreError(message="invalid json"))])


# --- Whens (each returns one doctor check's raw Result) ------------------------


@when("the doctor checks the identity", target_fixture="identity_result")
def check_identity_step(client: Client) -> Result[ServiceIdentity, str]:
    return async_run(check_identity(client))


@when("the doctor checks the captcha", target_fixture="captcha_result")
def check_captcha_step(captcha_handler: CaptchaHandler) -> Result[str, str]:
    image = load_captcha_image()
    assert image.is_ok  # setup: the packaged resource must exist for this check
    return async_run(check_captcha(captcha_handler, image.unwrap()))


@when("the doctor checks the scoring", target_fixture="scoring_result")
def check_scoring_step(scripted_ai: ScriptedAi) -> Result[ScoreResult, str]:
    vacancy = load_doctor_vacancy()
    assert vacancy.is_ok  # setup: the packaged resource must exist for this check
    return async_run(
        check_scoring(
            scripted_ai,
            resume="resume contents",
            prompt="Resume: {{ resume_formatted }} Vacancy: {{ vacancy_formatted }}",
            vacancy=vacancy.unwrap(),
        )
    )


@when("the doctor loads its vacancy resource", target_fixture="vacancy_loaded")
def load_vacancy_step() -> Result[DoctorVacancy, str]:
    return load_doctor_vacancy()


@given("a missing vacancy resource file", target_fixture="vacancy_path")
def missing_vacancy_path_step(tmp_path: Path) -> Path:
    return tmp_path / "missing.yaml"


@given("a vacancy resource with a wrong shape", target_fixture="vacancy_path")
def wrong_shape_vacancy_step(tmp_path: Path) -> Path:
    path = tmp_path / "wrong-shape.yaml"
    path.write_text("salary: 5\n", encoding="utf-8")  # valid YAML, missing title/description
    return path


@when("the doctor loads that vacancy resource", target_fixture="vacancy_loaded")
def load_vacancy_at_path_step(vacancy_path: Path) -> Result[DoctorVacancy, str]:
    return load_doctor_vacancy(vacancy_path)


@then("the vacancy load fails with a read error")
def vacancy_read_fail_step(vacancy_loaded: Result[DoctorVacancy, str]) -> None:
    assert vacancy_loaded.is_err
    assert "Cannot read" in vacancy_loaded.unwrap_err()


@then("the vacancy load fails validation")
def vacancy_invalid_fail_step(vacancy_loaded: Result[DoctorVacancy, str]) -> None:
    assert vacancy_loaded.is_err
    assert "Invalid doctor vacancy resource" in vacancy_loaded.unwrap_err()


@when("the doctor loads its captcha image resource", target_fixture="captcha_image")
def load_image_step() -> Result[bytes, str]:
    return load_captcha_image()


# --- Thens (assert only) -------------------------------------------------------


@then("the identity check succeeds with the fake identity")
def identity_ok_step(identity_result: Result[ServiceIdentity, str]) -> None:
    assert identity_result.is_ok
    identity = identity_result.unwrap()
    assert identity.external_id == "fake-user-1"
    assert identity.display_name == "Fake User"
    assert identity.email == "fake-user@example.com"


@then("the identity check fails")
def identity_fail_step(identity_result: Result[ServiceIdentity, str]) -> None:
    assert identity_result.is_err
    assert identity_result.unwrap_err() == "board rejected the account"


@then("the captcha check succeeds with a solved answer")
def captcha_ok_step(captcha_result: Result[str, str]) -> None:
    assert captcha_result.is_ok
    assert captcha_result.unwrap() == "1234"


@then("the captcha check fails")
def captcha_fail_step(captcha_result: Result[str, str]) -> None:
    assert captcha_result.is_err
    assert captcha_result.unwrap_err() == "captcha input ended (EOF)"


@then("the scoring check succeeds with score 4")
def scoring_ok_step(scoring_result: Result[ScoreResult, str]) -> None:
    assert scoring_result.is_ok
    score = scoring_result.unwrap()
    assert score.fit_score == 4
    assert score.comment == "good match"


@then("the scoring check fails")
def scoring_fail_step(scoring_result: Result[ScoreResult, str]) -> None:
    assert scoring_result.is_err
    assert scoring_result.unwrap_err() == "invalid json"


@then("the resource vacancy is a probe vacancy")
def vacancy_ok_step(vacancy_loaded: Result[DoctorVacancy, str]) -> None:
    assert vacancy_loaded.is_ok
    vacancy = vacancy_loaded.unwrap()
    assert vacancy.title == "Senior Python Developer (Doctor Probe)"
    assert vacancy.company == "Probe Systems"
    assert vacancy.description.strip() != ""


@then("the resource is a PNG image")
def image_ok_step(captcha_image: Result[bytes, str]) -> None:
    assert captcha_image.is_ok
    assert captcha_image.unwrap().startswith(b"\x89PNG")
