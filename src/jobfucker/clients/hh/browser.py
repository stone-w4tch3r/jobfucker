"""HH standalone CAPTCHA recovery through the shared browser engine.

The HH trust layer (docs/hh/captcha.md §5a) evaluates a standalone answer only
for a trusted browser context: pure-HTTP submissions are blanket-rejected as
``wrong_answer`` regardless of the answer. Recovery therefore runs a stealth
browser for the challenge window only — open the printed ``captcha_url``, pull
the challenge image out of the real page, hand it to the injected
:data:`CaptchaHandler` (AI vision or terminal), and submit the answer **through
the real page** so the site's own JS issues the GIB trust headers. The HTTP
client continues all other work; no cookie harvest back into httpx happens
(the observed unlock is tied to the account/Bearer state, not the jar).

Boundary: the generic driver/session/engine-install live in
:mod:`jobfucker.clients.shared.browser`; this module owns the **HH challenge
flow** — its DOM selectors, submit-path listener and same-origin success marker.
All selectors are HH standalone-challenge DOM markers (docs/hh/captcha.md,
website.md).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Final
from urllib.parse import parse_qs, urlparse

from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import CaptchaHandler, CaptchaSolvingError, ClientError, ProtocolError
from jobfucker.clients.hh.captcha import Challenge, log_captcha_image
from jobfucker.clients.shared.browser import (
    BROWSER_UNAVAILABLE_PREFIX,
    BrowserConfig,
    BrowserDriver,
    BrowserSession,
    ensure_chromium_engine,
)
from jobfucker.clients.shared.cookies import PersistedCookie

__all__ = [
    "BROWSER_UNAVAILABLE_PREFIX",
    "HH_BROWSER_CONFIG",
    "BrowserCaptchaSolver",
    "BrowserConfig",
    "BrowserDriver",
    "BrowserSession",
    "ensure_chromium_engine",
]

_PICTURE_SELECTOR: Final = 'img[data-qa="account-captcha-picture"]'
_INPUT_SELECTOR: Final = 'input[data-qa="account-captcha-input"]'
_SUBMIT_SELECTOR: Final = '[data-qa="account-captcha-submit"]'
_ERROR_SELECTOR: Final = '[data-qa="account-captcha-error"]'
_SUCCESS_SOURCE: Final = "account_captcha"
_MAX_ANSWER_CHARACTERS: Final = 64
_SUBMIT_TIMEOUT_S: Final = 15.0
_HTTP_MOVED_TEMPORARILY: Final = 302
_HTTP_FORBIDDEN: Final = 403

# The shared session listens for the HH challenge POST and resolves relative
# image URLs against the HH origin.
HH_BROWSER_CONFIG: Final = BrowserConfig(origin="https://hh.ru", challenge_post_path="/account/captcha")

logger = logging.getLogger(__name__)


class BrowserCaptchaSolver:
    """Solve one standalone challenge through the stealth browser engine."""

    def __init__(
        self,
        driver: BrowserDriver,
        handler: CaptchaHandler,
        *,
        max_attempts: int,
    ) -> None:
        self._driver = driver
        self._handler = handler
        self._max_attempts = max_attempts

    async def ensure_engine(self) -> Result[None, str]:
        """Preflight the automation stack before any board work starts."""
        return await self._driver.ensure_engine()

    async def solve(
        self,
        challenge: Challenge,
        *,
        cookies: Sequence[PersistedCookie],
    ) -> Result[None, ClientError]:
        """Clear ``challenge`` by solving and submitting inside the browser.

        The coordinator has already validated the recovery URL; the solver only
        accepts a standalone text challenge. Every failure carries the safe
        ``recovery_url`` so the human fallback (printed URL) stays available.
        """
        recovery_url = challenge.recovery_url
        if recovery_url is None:
            return Err(ProtocolError(message="HH standalone CAPTCHA has no recovery URL", status=challenge.status))
        try:
            driver_session = self._driver.session(headless=True)
            session = await driver_session.__aenter__()
        except Exception as exc:
            # Launch boundary: a missing engine binary or hostile environment
            # gets the actionable install message, not a raw automation error.
            logger.warning("browser captcha engine unavailable: %s: %s", type(exc).__name__, exc)
            return Err(
                CaptchaSolvingError(
                    message=f"{BROWSER_UNAVAILABLE_PREFIX} ({type(exc).__name__})",
                    recovery_url=recovery_url,
                )
            )
        try:
            return await self._solve_loop(challenge, session, cookies=cookies)
        except Exception as exc:
            # Interaction boundary: page drift, selector drift, or automation
            # failures surface with their real cause, never as "install".
            logger.warning("browser captcha interaction failed: %s: %s", type(exc).__name__, exc)
            return Err(
                CaptchaSolvingError(
                    message=f"Взаимодействие с браузером капчи не удалось: {type(exc).__name__}",
                    recovery_url=recovery_url,
                )
            )
        finally:
            await driver_session.__aexit__(None, None, None)

    async def _solve_loop(
        self,
        challenge: Challenge,
        session: BrowserSession,
        *,
        cookies: Sequence[PersistedCookie],
    ) -> Result[None, ClientError]:
        assert challenge.recovery_url is not None  # checked by solve()
        await session.import_cookies(cookies)
        for attempt in range(1, self._max_attempts + 1):
            await session.goto(challenge.recovery_url)
            png = await session.image_bytes(_PICTURE_SELECTOR)
            log_captcha_image(png, label="standalone")
            solved = await self._handler(png)
            if solved.is_err:
                return Err(
                    CaptchaSolvingError(
                        message=f"HH CAPTCHA solver failed: {solved.unwrap_err()}",
                        recovery_url=challenge.recovery_url,
                    )
                )
            answer = solved.unwrap().strip()
            if not answer or len(answer) > _MAX_ANSWER_CHARACTERS or not answer.isprintable():
                return Err(
                    CaptchaSolvingError(
                        message="HH CAPTCHA solver returned an invalid answer",
                        recovery_url=challenge.recovery_url,
                    )
                )
            logger.debug(
                "browser captcha attempt %d/%d: answer=%r",
                attempt,
                self._max_attempts,
                answer,
            )
            await session.fill(_INPUT_SELECTOR, answer)
            await session.click(_SUBMIT_SELECTOR)
            status = await session.submit_status(timeout_s=_SUBMIT_TIMEOUT_S)
            if status == _HTTP_MOVED_TEMPORARILY:
                return Ok(None)
            if status is None and await _cleared_via_page(session):
                return Ok(None)
            error_visible = await session.visible(_ERROR_SELECTOR)
            logger.debug(
                "browser captcha attempt %d/%d not cleared "
                "(submit_status=%s, answer_rejected=%s, error marker visible: %s)",
                attempt,
                self._max_attempts,
                status,
                status == _HTTP_FORBIDDEN,
                error_visible,
            )
        return Err(
            CaptchaSolvingError(
                message=f"HH CAPTCHA was not solved after {self._max_attempts} attempts",
                recovery_url=challenge.recovery_url,
            )
        )


async def _cleared_via_page(session: BrowserSession) -> bool:
    """Page-signal fallback for submits whose POST status was not observed.

    The current page always POSTs the answer, so this path only covers page
    drift (a non-XHR submit or a page that failed to issue the request). It
    keeps the strict same-origin ``hhtmFrom=account_captcha`` validation, so a
    foreign redirect cannot masquerade as success.
    """
    if not await session.wait_for_url(_SUCCESS_SOURCE, timeout_s=_SUBMIT_TIMEOUT_S):
        return False
    return _is_success_redirect(await session.current_url())


def _is_success_redirect(url: str) -> bool:
    """Validate the same-origin ``hhtmFrom=account_captcha`` success marker."""
    try:
        parsed = urlparse(url)
        sources = parse_qs(parsed.query).get("hhtmFrom", [])
        return (
            parsed.scheme == "https"
            and parsed.hostname == "hh.ru"
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and sources == [_SUCCESS_SOURCE]
        )
    except ValueError:
        return False
