"""Browser-click solver for the Habr Account login SmartCaptcha.

The primary captcha path: drive the shared stealth browser (a normal desktop
Chrome UA, never ``HeadlessChrome``) to the login page, wait out the widget's
spinner overlay, and perform a **real frame-local click** on the checkbox —
clicking the iframe element itself does not produce a token
(docs/habr/captcha.md). The widget then writes the ``spravka`` into the hidden
``input[name=smart-token]``, which this solver polls for.

Escalation (an advanced/image frame or a missing token) is detected, not
retried blindly: it is handed back so :class:`LoginCaptcha` can fall back to the
browserless vision solver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import CaptchaSolvingError, ClientError
from jobfucker.clients.habr.transport import ACCOUNT_ORIGIN, HABR_USER_AGENT
from jobfucker.clients.shared.browser import BrowserConfig, BrowserDriver, BrowserSession
from jobfucker.reporting import NullReporter, Reporter, RunEvent

__all__ = ["HABR_BROWSER_CONFIG", "BrowserClickSolver"]

# The UA override is mandatory: patchright's default ``HeadlessChrome`` string
# escalates SmartCaptcha to an image challenge (docs/habr/captcha.md).
HABR_BROWSER_CONFIG: Final = BrowserConfig(
    origin=ACCOUNT_ORIGIN,
    challenge_post_path="/check",
    locale="ru-RU",
    user_agent=HABR_USER_AGENT,
)

_SPINNER_SELECTOR: Final = ".SmartCaptcha-Overlay.SmartCaptcha-Overlay_show_spinner"
_CHECKBOX_FRAME_SELECTOR: Final = 'iframe[title="SmartCaptcha checkbox"]'
_FRAME_INPUT_SELECTOR: Final = "input"
_ADVANCED_FRAME_SELECTOR: Final = 'iframe[title="SmartCaptcha advanced"]'
# Hidden field the widget fills with the spravka after a pass (no secret here).
_SMART_CAPTCHA_INPUT: Final = 'input[name="smart-token"]'

_SPINNER_TIMEOUT_S: Final = 20.0
_CLICK_TIMEOUT_S: Final = 15.0
# The SmartCaptcha checkbox drops the first interaction on a freshly loaded page:
# the in-frame click registers, but the widget issues no ``/check`` until a later
# click (live-probed; the only POST before then is the widget's own autonomous
# precheck). Each solver attempt opens a fresh page, so one click per attempt
# never yields a token — re-click on the same page within this bound instead.
_CLICK_ROUNDS: Final = 3
_TOKEN_POLL_S: Final = 5.0


@dataclass(frozen=True, slots=True)
class _AttemptFailure:
    """Why one browser attempt did not yield a token."""

    message: str
    escalated: bool


class BrowserClickSolver:
    """Solve the login captcha by clicking the SmartCaptcha checkbox in a real browser."""

    def __init__(self, driver: BrowserDriver, *, max_attempts: int, reporter: Reporter | None = None) -> None:
        self._driver = driver
        self._max_attempts = max_attempts
        self._reporter = reporter if reporter is not None else NullReporter()

    async def ensure_engine(self) -> Result[None, str]:
        """Preflight the shared browser engine (voice for the caller on failure)."""
        return await self._driver.ensure_engine()

    async def solve(self, login_url: str) -> Result[str, ClientError]:
        """Click the checkbox and return the ``smart-token``, or ``Err`` on escalation/exhaustion."""
        last_reason = "the checkbox click produced no token"
        for _ in range(self._max_attempts):
            attempt = await self._attempt(login_url)
            if attempt.is_ok:
                return Ok(attempt.unwrap())
            failure = attempt.unwrap_err()
            await self._reporter.publish(
                RunEvent(
                    stage="captcha",
                    message=f"Habr: browser click attempt failed — {failure.message}",
                    level="warning",
                    url=login_url,
                )
            )
            if failure.escalated:
                return Err(CaptchaSolvingError(message=failure.message, recovery_url=login_url))
            last_reason = failure.message
        return Err(
            CaptchaSolvingError(
                message=f"Habr browser captcha did not produce a token: {last_reason}",
                recovery_url=login_url,
            )
        )

    async def _attempt(self, login_url: str) -> Result[str, _AttemptFailure]:
        await self._reporter.publish(
            RunEvent(stage="captcha", message="Habr: clicking the SmartCaptcha checkbox", level="debug", url=login_url)
        )
        try:
            # Third-party boundary: any launch/navigation/DOM failure is a failed
            # attempt (the vision solver is the fallback), never a raised error.
            async with self._driver.session(headless=True) as session:
                await session.goto(login_url)
                # The spinner overlay must clear before the click registers
                # (docs/habr/captcha.md gotcha); a timeout here still tries the click.
                await session.wait_for_hidden(_SPINNER_SELECTOR, timeout_s=_SPINNER_TIMEOUT_S)
                token = await self._click_until_token(session)
                if token:
                    return Ok(token)
                if await session.visible(_ADVANCED_FRAME_SELECTOR):
                    return Err(_AttemptFailure("SmartCaptcha escalated to the advanced challenge", escalated=True))
                return Err(_AttemptFailure("the checkbox click produced no token", escalated=False))
        except Exception as exc:
            detail = str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__
            return Err(_AttemptFailure(f"browser session failed: {type(exc).__name__}: {detail}", escalated=False))

    async def _click_until_token(self, session: BrowserSession) -> str:
        """Click the checkbox, re-clicking while the widget withholds its verdict.

        A single click on a fresh page is dropped by the widget (evidence in
        ``_CLICK_ROUNDS``); the shared ``click_in_frame`` already waits for the
        in-frame target to be interactive, and this bounded re-click gives the
        widget the interaction it accepts without opening another page. Returns
        the ``smart-token`` once it appears, or ``""`` when every round timed out.
        """
        token = ""
        for _ in range(_CLICK_ROUNDS):
            await session.click_in_frame(_CHECKBOX_FRAME_SELECTOR, _FRAME_INPUT_SELECTOR, timeout_s=_CLICK_TIMEOUT_S)
            token = await session.wait_for_value(_SMART_CAPTCHA_INPUT, timeout_s=_TOKEN_POLL_S) or ""
            if token:
                return token
        return token
