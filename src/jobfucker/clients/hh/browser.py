"""Browser-engine standalone CAPTCHA recovery (patchright stealth Chromium).

The HH trust layer (docs/hh/captcha.md §5a) evaluates a standalone answer only
for a trusted browser context: pure-HTTP submissions are blanket-rejected as
``wrong_answer`` regardless of the answer. Recovery therefore runs a stealth
browser for the challenge window only — open the printed ``captcha_url``, pull
the challenge PNG out of the real page, hand it to the injected
:data:`CaptchaHandler` (AI vision or terminal), and submit the answer **through
the real page** so the site's own JS issues the GIB trust headers. The HTTP
client continues all other work; no cookie harvest back into httpx happens
(the observed unlock is tied to the account/Bearer state, not the jar).

Boundary: :class:`BrowserDriver` is the narrow typed seam over the automation
stack (:class:`PatchrightDriver` today, a C++-patched drop-in later). Every
page interaction detail stays behind that seam, so detection drift is patched
in one class. All selectors are HH standalone-challenge DOM markers
(docs/hh/captcha.md, website.md).
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import TYPE_CHECKING, Final, Protocol
from urllib.parse import parse_qs, urlparse

import anyio
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import CaptchaHandler, CaptchaSolvingError, ClientError, ProtocolError
from jobfucker.clients.hh.captcha import Challenge, log_captcha_image
from jobfucker.clients.hh.models import PersistedCookie
from jobfucker.reporting import EventLevel, Reporter, RunEvent
from jobfucker.subprocess_utils import run_subprocess_with_capture

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext, Page, Response

logger = logging.getLogger(__name__)

_PICTURE_SELECTOR: Final = 'img[data-qa="account-captcha-picture"]'
_INPUT_SELECTOR: Final = 'input[data-qa="account-captcha-input"]'
_SUBMIT_SELECTOR: Final = '[data-qa="account-captcha-submit"]'
_ERROR_SELECTOR: Final = '[data-qa="account-captcha-error"]'
_SUCCESS_SOURCE: Final = "account_captcha"
_HH_ORIGIN: Final = "https://hh.ru"
_MAX_ANSWER_CHARACTERS: Final = 64
_GOTO_TIMEOUT_MS: Final = 30_000
_PICTURE_TIMEOUT_MS: Final = 20_000
_IMAGE_LOAD_TIMEOUT_MS: Final = 15_000
_SUBMIT_TIMEOUT_S: Final = 15.0
_HTTP_MOVED_TEMPORARILY: Final = 302
_HTTP_FORBIDDEN: Final = 403
_CHALLENGE_POST_PATH: Final = "/account/captcha"

# Belt and suspenders over patchright's own C-level patches: the validated
# gate clearer is a hidden ``navigator.webdriver`` (docs/hh/captcha.md §5a).
_WEBDRIVER_SPOOF: Final = "Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined });"

BROWSER_UNAVAILABLE_PREFIX: Final = (
    "Не удалось запустить браузер для решения капчи. Установите движок командой: patchright install chromium"
)

# Startup preflight (see :func:`ensure_chromium_engine`): the installer shells
# out to the patchright CLI, which downloads the engine on first use.
_INSTALL_TIMEOUT_S: Final = 300.0
# Captured installer output is not streamed; on failure the last lines go into
# the warning + run event, on success into the DEBUG file log.
_INSTALL_TAIL_LINES: Final = 20


class BrowserSession(Protocol):
    """One browser session prepared for a standalone challenge solve."""

    async def goto(self, url: str) -> None: ...

    async def import_cookies(self, cookies: Sequence[PersistedCookie]) -> None: ...

    async def image_bytes(self, selector: str) -> bytes:
        """Return the fully-loaded PNG bytes of the challenge image element."""
        ...

    async def fill(self, selector: str, text: str) -> None: ...

    async def click(self, selector: str) -> None: ...

    async def submit_status(self, *, timeout_s: float) -> int | None:
        """Return the challenge POST response status; ``None`` when not observed.

        The current page submits the answer as an XHR whose status is the
        authoritative verdict (302 cleared / 403 rejected), so listening for it
        beats racing the SPA navigation and the transient error marker.
        """
        ...

    async def current_url(self) -> str: ...

    async def wait_for_url(self, pattern: str, *, timeout_s: float) -> bool:
        """Wait until the page URL matches ``pattern``; ``False`` on timeout."""
        ...

    async def visible(self, selector: str) -> bool: ...


class BrowserDriver(Protocol):
    """Automation-stack seam: launches one stealth browser session."""

    async def ensure_engine(self) -> Result[None, str]:
        """Best-effort preflight: make the engine binary launchable.

        Runs at client startup (before the auth preflight), so a first run
        pays the engine download up front instead of mid-challenge. Never
        raises — an install failure is the ``Err`` reason, and the caller
        decides whether board work may proceed without the engine.
        """
        ...

    def session(self, *, headless: bool) -> AbstractAsyncContextManager[BrowserSession]:
        """Open one :class:`BrowserSession` as an async context manager."""
        ...


async def _announce(reporter: Reporter | None, level: EventLevel, message: str) -> None:
    """Publish one engine milestone on the run-event stream (reporters never raise)."""
    if reporter is not None:
        await reporter.publish(RunEvent(stage="client", message=message, level=level))


def _output_tail(*streams: bytes) -> str:
    """Last meaningful lines of captured installer output (progress-bar friendly).

    The installer draws \r-carriage progress bars: carriage returns become line
    breaks so earlier bar frames turn into dropped lines instead of one huge
    string.
    """
    text = "\n".join(stream.decode(errors="replace").replace("\r", "\n") for stream in streams)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines[-_INSTALL_TAIL_LINES:])


async def ensure_chromium_engine(executable_path: str, reporter: Reporter | None = None) -> Result[None, str]:
    """Install the patchright Chromium engine when ``executable_path`` is absent.

    Check-then-install: ``executable_path`` is the exact binary path the
    launcher will use (env-resolved, revision-pinned), so an existence check
    beats parsing launcher errors. Failures log + announce with the captured
    installer tail and come back as ``Err(reason)``; never raises.
    """
    if await anyio.Path(executable_path).is_file():
        return Ok(None)
    logger.info(
        "Browser engine binary missing at %s — installing patchright Chromium (first run only)...",
        executable_path,
    )
    await _announce(reporter, "info", "Browser engine not found — installing patchright Chromium (first run only)...")
    try:
        run = await run_subprocess_with_capture(
            (sys.executable, "-m", "patchright", "install", "chromium"),
            timeout_s=_INSTALL_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warning("browser engine auto-install crashed: %s: %s", type(exc).__name__, exc)
        await _announce(reporter, "warning", "Browser engine install failed — captcha solve will report the fix")
        return Err(f"{type(exc).__name__}: {exc}")
    tail = _output_tail(run.stdout, run.stderr)
    if run.timed_out:
        logger.warning("browser engine auto-install timed out after %ds", _INSTALL_TIMEOUT_S)
        if tail:
            logger.warning("browser engine installer output at timeout:\n%s", tail)
        reason = tail.splitlines()[-1] if tail else f"no output after {_INSTALL_TIMEOUT_S:.0f}s"
        await _announce(
            reporter, "warning", f"Browser engine install timed out — captcha solve will report the fix ({reason})"
        )
        return Err(f"install timed out after {_INSTALL_TIMEOUT_S:.0f}s ({reason})")
    if run.returncode != 0:
        logger.warning(
            "browser engine auto-install failed (exit %d):\n%s",
            run.returncode,
            tail,
        )
        reason = tail.splitlines()[-1] if tail else f"exit code {run.returncode}"
        await _announce(
            reporter, "warning", f"Browser engine install failed — captcha solve will report the fix ({reason})"
        )
        return Err(f"exit code {run.returncode} ({reason})")
    logger.debug("browser engine auto-install output:\n%s", tail)
    await _announce(reporter, "success", "Browser engine installed")
    return Ok(None)


class PatchrightDriver:
    """:class:`BrowserDriver` over patchright (patched Playwright Chromium)."""

    def __init__(self, reporter: Reporter | None = None) -> None:
        self._reporter = reporter
        self._engine_result: Result[None, str] | None = None

    async def ensure_engine(self) -> Result[None, str]:
        """Preflight the engine once per driver; never raises (see protocol)."""
        if self._engine_result is not None:
            return self._engine_result
        try:
            from patchright.async_api import async_playwright

            async with async_playwright() as playwright:
                result = await ensure_chromium_engine(playwright.chromium.executable_path, reporter=self._reporter)
        except Exception as exc:
            result: Result[None, str] = Err(f"{type(exc).__name__}: {exc}")
        # Memoize the outcome, not just success: a broken engine fails every
        # action identically instead of re-attempting a doomed install.
        self._engine_result = result
        return result

    @asynccontextmanager
    async def session(self, *, headless: bool) -> AsyncGenerator[PatchrightSession]:
        # Lazy import: the patchright stack is only paid when a challenge fires.
        from patchright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=headless)
            try:
                context = await browser.new_context(locale="ru-RU")
                await context.add_init_script(_WEBDRIVER_SPOOF)
                page = await context.new_page()
                yield PatchrightSession(context, page)
            finally:
                await browser.close()


class PatchrightSession:
    """:class:`BrowserSession` over one patchright browser context."""

    def __init__(self, context: BrowserContext, page: Page) -> None:
        self._context = context
        self._page = page
        self._submit_status: int | None = None
        self._submit_seen = asyncio.Event()
        page.on("response", self._record_submit_response)

    def _record_submit_response(self, response: Response) -> None:
        """Remember the challenge POST status; the page submits it as an XHR.

        Only the challenge POST matters (picture/key GETs and the site's own
        telemetry share the origin), and only its status is kept: the verdict
        is 302 = cleared, 403 = rejected answer.
        """
        if response.request.method == "POST" and _CHALLENGE_POST_PATH in response.url:
            self._submit_status = response.status
            self._submit_seen.set()

    async def goto(self, url: str) -> None:
        await self._page.goto(url, wait_until="domcontentloaded", timeout=_GOTO_TIMEOUT_MS)

    async def import_cookies(self, cookies: Sequence[PersistedCookie]) -> None:
        converted: list[dict[str, str | int | bool]] = []
        for cookie in cookies:
            entry: dict[str, str | int | bool] = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain.lstrip("."),
                "path": cookie.path,
                "secure": cookie.secure,
                "httpOnly": False,
                "sameSite": "Lax",
            }
            if cookie.expires is not None:
                entry["expires"] = cookie.expires
            converted.append(entry)
        await self._context.add_cookies(converted)  # type: ignore[reportArgumentType]  # rationale: patchright's SetCookieParam TypedDict is not publicly exported; the dict shape matches it exactly (name/value/domain/path/secure/httpOnly/sameSite/expires)

    async def image_bytes(self, selector: str) -> bytes:
        """Wait for the challenge image to actually load, then fetch its bytes.

        The ``<img>`` may render before its ``src`` resolves (observed in the
        PoC: a bare screenshot captured the broken-image placeholder), so the
        element is only read once ``naturalWidth > 0``. The original PNG bytes
        are fetched through the browser context (same jar, same trust) instead
        of a rendered screenshot.
        """
        locator = self._page.locator(selector)
        await locator.wait_for(state="visible", timeout=_PICTURE_TIMEOUT_MS)
        await self._page.wait_for_function(
            "selector => { const img = document.querySelector(selector);"
            " return !!img && img.complete && img.naturalWidth > 0; }",
            arg=selector,
            timeout=_IMAGE_LOAD_TIMEOUT_MS,
        )
        source = await locator.get_attribute("src")
        if source:
            url = source if source.startswith("http") else f"{_HH_ORIGIN}{source}"
            response = await self._context.request.get(url)
            content_type = response.headers.get("content-type", "")
            if response.ok and content_type.startswith("image/"):
                return await response.body()
        return await locator.screenshot()

    async def fill(self, selector: str, text: str) -> None:
        await self._page.locator(selector).fill(text)

    async def click(self, selector: str) -> None:
        # The verdict listener only cares about the POST this click triggers.
        self._submit_status = None
        self._submit_seen.clear()
        await self._page.locator(selector).click()

    async def submit_status(self, *, timeout_s: float) -> int | None:
        try:
            async with asyncio.timeout(timeout_s):
                await self._submit_seen.wait()
        except TimeoutError:
            return None
        return self._submit_status

    async def current_url(self) -> str:
        return self._page.url

    async def wait_for_url(self, pattern: str, *, timeout_s: float) -> bool:
        try:
            await self._page.wait_for_url(re.compile(pattern), timeout=int(timeout_s * 1000))
        except Exception:
            # patchright raises its own Error hierarchy on timeout; the verdict
            # is the boolean return, not the exception type.
            return False
        return True

    async def visible(self, selector: str) -> bool:
        return await self._page.locator(selector).is_visible()


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
