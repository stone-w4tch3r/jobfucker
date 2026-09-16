"""Scripted :class:`BrowserDriver` test double for standalone CAPTCHA scenarios.

The production engine (``jobfucker.clients.hh.browser``) opens the challenge in
a stealth browser, solves the page image through the injected
:class:`CaptchaHandler`, and submits through the real page. This double replays
the same protocol deterministically: each scripted submit outcome sets the
challenge POST status (302/403, the production verdict) and flips the page URL
to the matching state, exactly as the real page would.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Final, Literal

from rusty_results.prelude import Ok, Result

from jobfucker.clients.hh.models import PersistedCookie

SubmitOutcome = Literal["success", "foreign", "error"]

SUCCESS_URL: Final = "https://hh.ru/?hhtmFrom=account_captcha"
FOREIGN_URL: Final = "https://example.invalid/?hhtmFrom=account_captcha"
ERROR_URL: Final = "https://hh.ru/account/captcha?state=challenge-state"

# The production page submits the answer via XHR: 302 = cleared, 403 = rejected.
# ``foreign`` simulates a page-drift case with no challenge POST observed, so
# only the strict URL validation can decide the verdict.
_OUTCOME_STATUS: Final[dict[SubmitOutcome, int | None]] = {
    "success": 302,
    "foreign": None,
    "error": 403,
}
_OUTCOME_URL: Final[dict[SubmitOutcome, str]] = {
    "success": SUCCESS_URL,
    "foreign": FOREIGN_URL,
    "error": ERROR_URL,
}

_PNG: Final[bytes] = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class FakeBrowserSession:
    """One scripted challenge page: records interactions, flips URL per submit."""

    _last_filled: str = ""

    def __init__(self, outcomes: list[SubmitOutcome], image: bytes) -> None:
        self._outcomes = list(outcomes)
        self._image = image
        self.pages_opened = 0
        self.cookies_imported: list[str] = []
        self.images_served = 0
        self.served_images: list[bytes] = []
        self.answers_submitted: list[str] = []
        self.url = ""
        self._submit_status: int | None = None

    async def goto(self, url: str) -> None:
        self.pages_opened += 1
        self.url = url

    async def import_cookies(self, cookies: Sequence[PersistedCookie]) -> None:
        self.cookies_imported = [cookie.name for cookie in cookies]

    async def image_bytes(self, selector: str) -> bytes:
        del selector
        self.images_served += 1
        self.served_images.append(self._image)
        return self._image

    async def fill(self, selector: str, text: str) -> None:
        del selector
        self._last_filled = text

    async def click(self, selector: str) -> None:
        del selector
        outcome: SubmitOutcome = self._outcomes.pop(0) if self._outcomes else "error"
        self._submit_status = _OUTCOME_STATUS[outcome]
        self.answers_submitted.append(self._last_filled)
        self.url = _OUTCOME_URL[outcome]

    async def submit_status(self, *, timeout_s: float) -> int | None:
        del timeout_s
        return self._submit_status

    async def current_url(self) -> str:
        return self.url

    async def wait_for_url(self, pattern: str, *, timeout_s: float) -> bool:
        del timeout_s
        return pattern in self.url

    async def visible(self, selector: str) -> bool:
        del selector
        return self.url == ERROR_URL


class FakeBrowserDriver:
    """Driver double yielding scripted sessions; records every session opened."""

    def __init__(self, outcomes: list[SubmitOutcome], image: bytes = _PNG) -> None:
        self._outcomes = outcomes
        self._image = image
        self.sessions: list[FakeBrowserSession] = []
        self.headless_flags: list[bool] = []
        self.ensure_engine_calls = 0

    async def ensure_engine(self) -> Result[None, str]:
        """Successful no-op preflight: the scripted double has no engine."""
        self.ensure_engine_calls += 1
        return Ok(None)

    def session(self, *, headless: bool) -> AbstractAsyncContextManager[FakeBrowserSession]:
        self.headless_flags.append(headless)
        session = FakeBrowserSession(self._outcomes, self._image)
        self.sessions.append(session)

        @asynccontextmanager
        async def _context() -> AsyncGenerator[FakeBrowserSession]:
            yield session

        return _context()

    @property
    def answers_submitted(self) -> list[str]:
        return [answer for session in self.sessions for answer in session.answers_submitted]

    @property
    def images_served(self) -> list[bytes]:
        return [image for session in self.sessions for image in session.served_images]
