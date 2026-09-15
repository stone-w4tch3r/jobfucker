"""BDD acceptance coverage for HH CAPTCHA classification, browser-engine solving, and replay bounds.

Step state flows by fixture injection (test/AGENTS.md §3b): the ``Given`` seeds
a frozen ``CaptchaScenario`` (data-configured transport + recorders), the
``When`` returns a frozen ``CaptchaOutcome``, and ``Then`` steps only assert.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from pytest_bdd import given, scenarios, then, when
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import CaptchaSolvingError, ClientError, ProtocolError
from jobfucker.clients.hh.client import HHClient
from jobfucker.clients.hh.config import HHSearchEntry, HHServiceConfig
from jobfucker.testing.step_runner import async_run
from test.clients.hh.browser_fake import FakeBrowserDriver, SubmitOutcome
from test.clients.hh.helpers import (
    RecordingSolver,
    StubAuthInteraction,
    applicant_healthcheck,
    hh_client,
    seed_auth_state,
)

scenarios("bdd/captcha.feature")

_LOGIN = "captcha@example.test"
_PROFILE_ID = hashlib.sha256(_LOGIN.encode()).hexdigest()
_RECOVERY_URL = "https://hh.ru/account/captcha?state=challenge-state"


class CaptchaTransport:
    """HH boundary double: the healthcheck serves the scripted challenge; nothing else is HTTP."""

    def __init__(self, *, challenge: httpx.Response, challenge_every: bool = False) -> None:
        self.requests: list[httpx.Request] = []
        self._challenge = challenge
        self._challenge_every = challenge_every

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "api.hh.ru" and request.url.path == "/me":
            if self._challenge_every or len(self.requests) == 1:
                return self._challenge
            return applicant_healthcheck()
        raise AssertionError(f"Unexpected HH request: {request.method} {request.url}")


def _challenge(recovery_url: str = _RECOVERY_URL) -> httpx.Response:
    return httpx.Response(
        403,
        headers={"content-type": "application/json", "x-request-id": "request-safe-id"},
        json={"errors": [{"type": "captcha_required", "value": "captcha_required", "captcha_url": recovery_url}]},
    )


def _recaptcha_challenge() -> httpx.Response:
    return httpx.Response(403, content=b'<textarea name="g-recaptcha-response"></textarea>')


def _unknown_challenge() -> httpx.Response:
    return httpx.Response(403, content=b'<div class="captcha-provider-drift"></div>')


@dataclass(frozen=True, slots=True)
class CaptchaScenario:
    """Frozen setup: the client factory plus the interaction recorders to assert on."""

    transport: CaptchaTransport
    make_client: Callable[[], HHClient]
    driver: FakeBrowserDriver
    solver: RecordingSolver


@dataclass(frozen=True, slots=True)
class CaptchaOutcome:
    """Frozen outcome: the authorization error and the request history it produced."""

    error: ClientError | None
    requests: tuple[httpx.Request, ...]


def _scenario(
    tmp_path: Path,
    *,
    challenge: httpx.Response,
    outcomes: tuple[SubmitOutcome, ...] = (),
    challenge_every: bool = False,
    max_attempts: int = 3,
    solver: RecordingSolver | None = None,
) -> CaptchaScenario:
    transport = CaptchaTransport(challenge=challenge, challenge_every=challenge_every)
    driver = FakeBrowserDriver(list(outcomes))
    solver = solver if solver is not None else RecordingSolver(Ok("answer-1"), Ok("answer-2"), Ok("answer-3"))
    data_dir = tmp_path / "data"
    seed_auth_state(data_dir, _PROFILE_ID, access_token="USER-captcha", refresh_token="refresh-captcha")
    rejected_code: Result[str, str] = Err("unexpected code request")
    confirmed: bool = False
    rejected_confirm: Result[bool, str] = Ok(confirmed)

    def make_client() -> HHClient:
        return hh_client(
            data_dir=data_dir,
            profile_id=_PROFILE_ID,
            transport=httpx.MockTransport(transport),
            config=HHServiceConfig(
                resume_id="resume-1",
                searches=(HHSearchEntry(query="python"),),
                captcha_max_attempts=max_attempts,
            ),
            solver=solver,
            auth_interaction=StubAuthInteraction(code=rejected_code, confirm=rejected_confirm),
            browser_driver=driver,
            login=_LOGIN,
            password="password",
        )

    return CaptchaScenario(transport=transport, make_client=make_client, driver=driver, solver=solver)


@given("a persisted HH token blocked by a standalone text CAPTCHA", target_fixture="captcha_scenario")
def blocked_step(tmp_path: Path) -> CaptchaScenario:
    return _scenario(tmp_path, challenge=_challenge(), outcomes=("success",))


@given("a standalone text CAPTCHA rejecting its first answer", target_fixture="captcha_scenario")
def wrong_then_solved_step(tmp_path: Path) -> CaptchaScenario:
    return _scenario(tmp_path, challenge=_challenge(), outcomes=("error", "success"))


@given("a standalone text CAPTCHA rejecting every answer", target_fixture="captcha_scenario")
def exhausted_step(tmp_path: Path) -> CaptchaScenario:
    return _scenario(tmp_path, challenge=_challenge(), outcomes=("error", "error", "error"))


@given("a standalone text CAPTCHA configured for one rejected answer", target_fixture="captcha_scenario")
def configured_one_attempt_step(tmp_path: Path) -> CaptchaScenario:
    return _scenario(tmp_path, challenge=_challenge(), outcomes=("error",), max_attempts=1)


@given("a standalone text CAPTCHA whose solver fails", target_fixture="captcha_scenario")
def solver_failure_step(tmp_path: Path) -> CaptchaScenario:
    return _scenario(tmp_path, challenge=_challenge(), solver=RecordingSolver(Err("human cancelled")))


@given("a CAPTCHA response containing a non-HH recovery URL", target_fixture="captcha_scenario")
def hostile_url_step(tmp_path: Path) -> CaptchaScenario:
    return _scenario(tmp_path, challenge=_challenge("https://example.invalid/account/captcha?state=challenge-state"))


@given("a standalone text CAPTCHA redirecting to a foreign origin after submit", target_fixture="captcha_scenario")
def foreign_redirect_step(tmp_path: Path) -> CaptchaScenario:
    return _scenario(tmp_path, challenge=_challenge(), outcomes=("foreign", "foreign", "foreign"))


@given("a standalone text CAPTCHA that returns after recovery", target_fixture="captcha_scenario")
def reentry_step(tmp_path: Path) -> CaptchaScenario:
    return _scenario(tmp_path, challenge=_challenge(), outcomes=("success",), challenge_every=True)


@given("a persisted HH token blocked by reCAPTCHA", target_fixture="captcha_scenario")
def recaptcha_step(tmp_path: Path) -> CaptchaScenario:
    return _scenario(tmp_path, challenge=_recaptcha_challenge())


@given("a persisted HH token blocked by an unknown CAPTCHA", target_fixture="captcha_scenario")
def unknown_captcha_step(tmp_path: Path) -> CaptchaScenario:
    return _scenario(tmp_path, challenge=_unknown_challenge())


async def _authorize_and_close(client: HHClient) -> ClientError | None:
    result = await client.authorize()
    await client.aclose()
    return result.unwrap_err() if result.is_err else None


@when("the HH client authorizes through CAPTCHA recovery", target_fixture="captcha_outcome")
def authorize_step(captcha_scenario: CaptchaScenario) -> CaptchaOutcome:
    error = async_run(_authorize_and_close(captcha_scenario.make_client()))
    return CaptchaOutcome(error=error, requests=tuple(captcha_scenario.transport.requests))


@then("CAPTCHA recovery succeeds")
def recovery_succeeds_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.error is None


@then("CAPTCHA recovery fails with its safe recovery URL")
def recovery_url_error_step(captcha_outcome: CaptchaOutcome) -> None:
    assert isinstance(captcha_outcome.error, CaptchaSolvingError)
    assert captcha_outcome.error.recovery_url == _RECOVERY_URL


@then("CAPTCHA recovery fails without a recovery URL")
def no_recovery_url_error_step(captcha_outcome: CaptchaOutcome) -> None:
    assert isinstance(captcha_outcome.error, CaptchaSolvingError)
    assert captcha_outcome.error.recovery_url is None


@then("CAPTCHA recovery fails with a protocol error")
def protocol_error_step(captcha_outcome: CaptchaOutcome) -> None:
    assert isinstance(captcha_outcome.error, ProtocolError)


@then("the browser engine submitted one answer through the real page")
def one_answer_step(captcha_scenario: CaptchaScenario) -> None:
    assert captcha_scenario.driver.answers_submitted == ["answer-1"]
    assert captcha_scenario.driver.sessions[0].pages_opened == 1
    assert captcha_scenario.driver.sessions[0].images_served == 1
    assert captcha_scenario.solver.images == captcha_scenario.driver.images_served


@then("the browser engine submitted two answers through the real page")
def two_answers_step(captcha_scenario: CaptchaScenario) -> None:
    assert captcha_scenario.driver.answers_submitted == ["answer-1", "answer-2"]
    assert captcha_scenario.driver.sessions[0].pages_opened == 2


@then("the browser engine submitted exactly three answers")
def three_answers_step(captcha_scenario: CaptchaScenario) -> None:
    assert len(captcha_scenario.driver.answers_submitted) == 3


@then("the browser engine submitted exactly one answer")
def exactly_one_answer_step(captcha_scenario: CaptchaScenario) -> None:
    assert len(captcha_scenario.driver.answers_submitted) == 1


@then("the browser engine submitted no answer")
def no_answer_step(captcha_scenario: CaptchaScenario) -> None:
    assert captcha_scenario.driver.answers_submitted == []


@then("the browser engine never opened")
def browser_never_opened_step(captcha_scenario: CaptchaScenario) -> None:
    assert captcha_scenario.driver.sessions == []
    assert captcha_scenario.solver.images == []


@then("the browser engine opened exactly one session")
def one_session_step(captcha_scenario: CaptchaScenario) -> None:
    assert len(captcha_scenario.driver.sessions) == 1


@then("the healthcheck was replayed once")
def replayed_once_step(captcha_outcome: CaptchaOutcome) -> None:
    assert sum(1 for request in captcha_outcome.requests if request.url.path == "/me") == 2


@then("the blocked healthcheck was not replayed")
def no_replay_step(captcha_outcome: CaptchaOutcome) -> None:
    assert sum(1 for request in captcha_outcome.requests if request.url.path == "/me") == 1


@then("the CAPTCHA solver received no image")
def solver_received_nothing_step(captcha_scenario: CaptchaScenario) -> None:
    assert captcha_scenario.solver.images == []
