"""BDD acceptance for the Habr SmartCaptcha protocol (test/AGENTS.md §3a, §3b).

The vision ladder runs against a routed ``httpx.MockTransport``; the browser
path runs against the scripted fake driver. The ``When`` returns a frozen
outcome carrying the solve result, the request history, the served images, the
frame clicks, and how often the browser was opened.
"""

from __future__ import annotations

import base64
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final

import httpx
from pytest_bdd import given, parsers, scenarios, then, when
from rusty_results.prelude import Ok, Result

from jobfucker.clients.base import CaptchaSolvingError, ClientError
from jobfucker.clients.habr.captcha import (
    BrowserClickSolver,
    HttpVisionSolver,
    PowSolution,
    decode_image_loader,
    encode_pdata,
    solve_pow,
)
from jobfucker.testing.step_runner import async_run
from test.clients.habr.browser_fake import FakeHabrBrowserDriver, FakeHabrBrowserSession
from test.clients.habr.helpers import (
    PNG_IMAGE,
    RecordingSolver,
    RoutedTransport,
    captcha_passed,
    checkbox_challenge,
    habr_captcha,
    habr_transport,
    image_challenge,
    image_loader,
    png_response,
    unknown_challenge,
)

scenarios("bdd/captcha.feature")

LOGIN_URL: Final = "https://account.habr.com/ru/ident/state-1"
SITEKEY: Final = "ysc1_test"
CHECK_HOST: Final = "smartcaptcha.cloud.yandex.ru"
IMAGE_HOST: Final = "img.smartcaptcha.yandexcloud.net"
IMAGE_URL: Final = f"https://{IMAGE_HOST}/image?key=k1"
SPRAVKA: Final = "spravka-token"
BROWSER_TOKEN: Final = "browser-captcha-token"
SPINNER_SELECTOR: Final = ".SmartCaptcha-Overlay.SmartCaptcha-Overlay_show_spinner"
SMART_TOKEN_SELECTOR: Final = 'input[name="smart-token"]'

SolveCall = Callable[[], Awaitable[Result[str, ClientError]]]


@dataclass(frozen=True, slots=True)
class CaptchaSetup:
    """Frozen setup: the solve call plus the doubles to inspect afterwards."""

    run: SolveCall
    routed: RoutedTransport
    solver: RecordingSolver
    driver: FakeHabrBrowserDriver
    session: FakeHabrBrowserSession | None


@dataclass(frozen=True, slots=True)
class CaptchaOutcome:
    """Frozen outcome: the solve result and the observable side effects."""

    result: Result[str, ClientError]
    requests: tuple[httpx.Request, ...]
    images: tuple[bytes, ...]
    frame_clicks: tuple[tuple[str, str], ...]
    browser_opens: int
    ensure_engine_calls: int
    driver_events: tuple[str, ...]
    hidden_selectors: tuple[str, ...]
    value_selectors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PowOutcome:
    """Frozen outcome: the solved nonce together with its inputs."""

    solution: PowSolution
    prefix: str
    complexity: int


def _browser_setup(session: FakeHabrBrowserSession) -> CaptchaSetup:
    driver = FakeHabrBrowserDriver(session)
    solver = RecordingSolver()
    browser = BrowserClickSolver(driver, max_attempts=2)
    return CaptchaSetup(
        run=lambda: browser.solve(LOGIN_URL),
        routed=RoutedTransport(),
        solver=solver,
        driver=driver,
        session=session,
    )


def _vision_setup(
    *,
    checks: tuple[httpx.Response, ...],
    answers: tuple[Result[str, str], ...],
    max_attempts: int,
) -> CaptchaSetup:
    routed = RoutedTransport()
    routed.on("POST", CHECK_HOST, "/check", *checks)
    routed.on("GET", IMAGE_HOST, "/image", png_response())
    solver = RecordingSolver(*answers)
    vision = HttpVisionSolver(habr_transport(routed), solver, max_attempts=max_attempts)
    return CaptchaSetup(
        run=lambda: vision.solve(LOGIN_URL, SITEKEY),
        routed=routed,
        solver=solver,
        driver=FakeHabrBrowserDriver(),
        session=None,
    )


def _coordinator_setup() -> CaptchaSetup:
    routed = RoutedTransport()
    routed.on("POST", CHECK_HOST, "/check", image_challenge("k1", IMAGE_URL), captcha_passed(SPRAVKA))
    routed.on("GET", IMAGE_HOST, "/image", png_response())
    solver = RecordingSolver(Ok("abcde"))
    driver = FakeHabrBrowserDriver()  # no session -> the browser attempt fails
    captcha = habr_captcha(habr_transport(routed), solver, driver=driver, max_attempts=2)
    return CaptchaSetup(
        run=lambda: captcha.solve(LOGIN_URL, SITEKEY),
        routed=routed,
        solver=solver,
        driver=driver,
        session=None,
    )


def _coordinator_browser_setup(
    session: FakeHabrBrowserSession | None,
    *,
    engine_error: str | None = None,
) -> CaptchaSetup:
    """A coordinator over a browser double, with a vision ladder behind it."""
    routed = RoutedTransport()
    routed.on("POST", CHECK_HOST, "/check", image_challenge("k1", IMAGE_URL), captcha_passed(SPRAVKA))
    routed.on("GET", IMAGE_HOST, "/image", png_response())
    solver = RecordingSolver(Ok("abcde"))
    driver = FakeHabrBrowserDriver(session, engine_error=engine_error)
    captcha = habr_captcha(habr_transport(routed), solver, driver=driver, max_attempts=2)
    return CaptchaSetup(
        run=lambda: captcha.solve(LOGIN_URL, SITEKEY),
        routed=routed,
        solver=solver,
        driver=driver,
        session=session,
    )


@given(
    parsers.parse('a browser SmartCaptcha page whose checkbox yields token "{token}"'), target_fixture="captcha_setup"
)
def browser_token_step(token: str) -> CaptchaSetup:
    return _browser_setup(FakeHabrBrowserSession(token=token))


@given("a browser SmartCaptcha page that escalates to the advanced challenge", target_fixture="captcha_setup")
def browser_escalated_step() -> CaptchaSetup:
    return _browser_setup(FakeHabrBrowserSession(token=None, advanced_visible=True))


@given(
    "a browser SmartCaptcha page whose token appears only on the second click",
    target_fixture="captcha_setup",
)
def browser_second_click_step() -> CaptchaSetup:
    return _browser_setup(FakeHabrBrowserSession(token=BROWSER_TOKEN, token_after_clicks=2))


@given("a vision captcha ladder whose checkbox escalates into a pass", target_fixture="captcha_setup")
def vision_checkbox_to_pass_step() -> CaptchaSetup:
    return _vision_setup(
        checks=(checkbox_challenge("k1"), captcha_passed(SPRAVKA)),
        answers=(Ok("abcde"),),
        max_attempts=2,
    )


@given(
    parsers.parse('a vision captcha ladder that offers a checkbox then an image solved with "{answer}"'),
    target_fixture="captcha_setup",
)
def vision_checkbox_then_image_step(answer: str) -> CaptchaSetup:
    return _vision_setup(
        checks=(checkbox_challenge("k1"), image_challenge("k2", IMAGE_URL), captcha_passed(SPRAVKA)),
        answers=(Ok(answer),),
        max_attempts=4,
    )


@given(
    parsers.parse('a vision captcha ladder that rejects "{wrong}" then accepts "{right}"'),
    target_fixture="captcha_setup",
)
def vision_wrong_then_right_step(wrong: str, right: str) -> CaptchaSetup:
    return _vision_setup(
        checks=(image_challenge("k1", IMAGE_URL), image_challenge("k2", IMAGE_URL), captcha_passed(SPRAVKA)),
        answers=(Ok(wrong), Ok(right)),
        max_attempts=4,
    )


@given(
    parsers.parse("a vision captcha ladder that rejects every answer with {max_attempts:d} attempts allowed"),
    target_fixture="captcha_setup",
)
def vision_exhausted_step(max_attempts: int) -> CaptchaSetup:
    return _vision_setup(
        checks=(image_challenge("k1", IMAGE_URL), image_challenge("k2", IMAGE_URL)),
        answers=(Ok("wrong"),),
        max_attempts=max_attempts,
    )


@given("a vision captcha ladder that offers an unknown challenge", target_fixture="captcha_setup")
def vision_unknown_step() -> CaptchaSetup:
    return _vision_setup(checks=(unknown_challenge("k1"),), answers=(Ok("abcde"),), max_attempts=2)


@given("a vision captcha ladder whose checkbox demands an implausible proof-of-work", target_fixture="captcha_setup")
def vision_implausible_pow_step() -> CaptchaSetup:
    return _vision_setup(checks=(checkbox_challenge("k1", complexity=40),), answers=(Ok("abcde"),), max_attempts=2)


@given('a failing browser solver and a vision ladder solved with "abcde"', target_fixture="captcha_setup")
def coordinator_fallback_step() -> CaptchaSetup:
    return _coordinator_setup()


@given(
    parsers.parse('a coordinator with a working browser whose checkbox yields token "{token}"'),
    target_fixture="captcha_setup",
)
def coordinator_browser_step(token: str) -> CaptchaSetup:
    return _coordinator_browser_setup(FakeHabrBrowserSession(token=token))


@given("a coordinator whose browser engine is unavailable", target_fixture="captcha_setup")
def coordinator_engine_unavailable_step() -> CaptchaSetup:
    return _coordinator_browser_setup(None, engine_error="engine missing")


@given(
    parsers.parse('a proof-of-work prefix of "{prefix}" with complexity {complexity:d}'),
    target_fixture="pow_input",
)
def pow_input_step(prefix: str, complexity: int) -> tuple[str, int]:
    return (prefix, complexity)


@given(parsers.parse('the widget image loader for "{image_url}"'), target_fixture="loader")
def image_loader_step(image_url: str) -> str:
    return image_loader(image_url)


@when("the browser solver solves the login captcha", target_fixture="captcha_outcome")
def run_browser_step(captcha_setup: CaptchaSetup) -> CaptchaOutcome:
    return _run(captcha_setup)


@when("the vision solver solves the login captcha", target_fixture="captcha_outcome")
def run_vision_step(captcha_setup: CaptchaSetup) -> CaptchaOutcome:
    return _run(captcha_setup)


@when("the login captcha coordinator solves the login captcha", target_fixture="captcha_outcome")
def run_coordinator_step(captcha_setup: CaptchaSetup) -> CaptchaOutcome:
    return _run(captcha_setup)


@when("the proof-of-work is solved", target_fixture="pow_outcome")
def solve_pow_step(pow_input: tuple[str, int]) -> PowOutcome:
    prefix, complexity = pow_input
    solution = solve_pow(prefix, complexity)
    assert solution is not None
    return PowOutcome(solution=solution, prefix=prefix, complexity=complexity)


@when("the loader is decoded", target_fixture="decoded_url")
def decode_loader_step(loader: str) -> str:
    decoded = decode_image_loader(loader)
    assert decoded is not None
    return decoded


def _run(setup: CaptchaSetup) -> CaptchaOutcome:
    result = async_run(setup.run())
    clicks = tuple(setup.session.frame_clicks) if setup.session is not None else ()
    hidden_selectors = tuple(setup.session.hidden_selectors) if setup.session is not None else ()
    value_selectors = tuple(setup.session.value_selectors) if setup.session is not None else ()
    return CaptchaOutcome(
        result=result,
        requests=tuple(setup.routed.requests),
        images=tuple(setup.solver.images),
        frame_clicks=clicks,
        browser_opens=setup.driver.opens,
        ensure_engine_calls=setup.driver.ensure_engine_calls,
        driver_events=tuple(setup.driver.events),
        hidden_selectors=hidden_selectors,
        value_selectors=value_selectors,
    )


@then(parsers.parse('the solution is "{token}"'))
def assert_solution_step(captcha_outcome: CaptchaOutcome, token: str) -> None:
    assert captcha_outcome.result.is_ok
    assert captcha_outcome.result.unwrap() == token


@then("the solution is the spravka token")
def assert_spravka_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.result.is_ok
    assert captcha_outcome.result.unwrap() == SPRAVKA


@then("the solution fails with a CAPTCHA error")
def assert_captcha_error_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.result.is_err
    assert isinstance(captcha_outcome.result.unwrap_err(), CaptchaSolvingError)


@then("the solution fails with an escalation CAPTCHA error")
def assert_escalation_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.result.is_err
    error = captcha_outcome.result.unwrap_err()
    assert isinstance(error, CaptchaSolvingError)
    assert "escalat" in error.message.lower()


@then("the browser engine was preflighted exactly once")
def assert_engine_preflight_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.ensure_engine_calls == 1


@then("the browser engine was preflighted before the browser attempt")
def assert_engine_preflight_order_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.driver_events == ("ensure_engine", "session")


@then("the browser was opened exactly once")
def assert_browser_once_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.browser_opens == 1


@then("the browser was never opened")
def assert_browser_never_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.browser_opens == 0


@then("the solution fails with a CAPTCHA error carrying the login URL")
def assert_captcha_recovery_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.result.is_err
    error = captcha_outcome.result.unwrap_err()
    assert isinstance(error, CaptchaSolvingError)
    assert error.recovery_url == LOGIN_URL


@then("the solver clicked the checkbox inside the SmartCaptcha frame")
def assert_frame_click_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.frame_clicks
    frame_selector, inner_selector = captcha_outcome.frame_clicks[0]
    assert "SmartCaptcha checkbox" in frame_selector
    assert inner_selector == "input"


@then("the solver clicked the checkbox twice")
def assert_two_clicks_step(captcha_outcome: CaptchaOutcome) -> None:
    assert len(captcha_outcome.frame_clicks) == 2


@then("the solver submitted one captcha image")
def assert_one_image_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.images == (PNG_IMAGE,)


@then("the solver submitted two captcha images")
def assert_two_images_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.images == (PNG_IMAGE, PNG_IMAGE)


@then("no captcha image was submitted")
def assert_no_image_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.images == ()


@then("the vision ladder ran after the browser attempt")
def assert_vision_after_browser_step(captcha_outcome: CaptchaOutcome) -> None:
    # The browser solver exhausts both configured attempts before the fallback.
    assert captcha_outcome.browser_opens == 2
    assert captcha_outcome.images == (PNG_IMAGE,)


@then("the solver awaited the SmartCaptcha spinner and the smart-token field")
def assert_selector_requests_step(captcha_outcome: CaptchaOutcome) -> None:
    assert captcha_outcome.hidden_selectors == (SPINNER_SELECTOR,)
    assert captcha_outcome.value_selectors == (SMART_TOKEN_SELECTOR,)


@then("the vision solver made exactly one check request")
def assert_one_check_step(captcha_outcome: CaptchaOutcome) -> None:
    checks = [request for request in captcha_outcome.requests if request.url.path == "/check"]
    assert len(checks) == 1


@then(parsers.parse("the nonce is 16 bytes and the hash has at least {complexity:d} leading zero bits"))
def assert_pow_step(pow_outcome: PowOutcome, complexity: int) -> None:
    nonce = bytes.fromhex(pow_outcome.solution.nonce)
    assert len(nonce) == 16
    digest = hashlib.sha256(bytes.fromhex(pow_outcome.prefix) + nonce).digest()
    assert _leading_zero_bits(digest) >= complexity


@then("the encoded pdata carries the nonce, the prefix, and a positive calc time")
def assert_pdata_step(pow_outcome: PowOutcome) -> None:
    assert pow_outcome.solution.calc_time_ms >= 1
    pdata = encode_pdata(pow_outcome.solution, pow_outcome.prefix)
    padded = pdata + "=" * (-len(pdata) % 4)
    text = base64.urlsafe_b64decode(padded).decode("ascii")
    assert f'"powNonce":"{pow_outcome.solution.nonce}"' in text
    assert f'"powPrefix":"{pow_outcome.prefix}"' in text
    assert '"powCalcTime":' in text


@then(parsers.parse('the decoded image URL is "{expected}"'))
def assert_decoded_url_step(decoded_url: str, expected: str) -> None:
    assert decoded_url == expected


def _leading_zero_bits(digest: bytes) -> int:
    count = 0
    for byte in digest:
        if byte == 0:
            count += 8
            continue
        count += 8 - byte.bit_length()
        break
    return count
