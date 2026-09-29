"""Scripted browser double for the Habr login-captcha BDD scenarios.

Satisfies the widened shared ``BrowserSession`` / ``BrowserDriver`` protocols
with scripted no-ops; only the methods the Habr click solver calls carry
behavior. Never spawns a real node driver or downloads an engine.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.shared.browser import BrowserSession
from jobfucker.clients.shared.cookies import PersistedCookie


class FakeHabrBrowserSession:
    """A scripted SmartCaptcha page: spinner state, click target, token value."""

    def __init__(
        self,
        *,
        token: str | None = None,
        spinner_hidden: bool = True,
        advanced_visible: bool = False,
        fail_on_goto: bool = False,
        token_after_clicks: int = 1,
    ) -> None:
        self.token = token
        self.spinner_hidden = spinner_hidden
        self.advanced_visible = advanced_visible
        self.fail_on_goto = fail_on_goto
        # Live SmartCaptcha drops the first click on a fresh page; the widget
        # only answers a later click. Modelled here so the solver's re-click is
        # observable: the token surfaces only once this many clicks have landed.
        self.token_after_clicks = token_after_clicks
        self.visited: list[str] = []
        self.frame_clicks: list[tuple[str, str]] = []
        self.hidden_selectors: list[str] = []
        self.value_selectors: list[str] = []

    async def goto(self, url: str) -> None:
        if self.fail_on_goto:
            raise RuntimeError("navigation failed")
        self.visited.append(url)

    async def import_cookies(self, cookies: Sequence[PersistedCookie]) -> None:
        del cookies

    async def image_bytes(self, selector: str) -> bytes:
        raise AssertionError(f"Habr click solver must not read images ({selector})")

    async def fill(self, selector: str, text: str) -> None:
        raise AssertionError(f"Habr click solver must not fill ({selector}={text})")

    async def click(self, selector: str) -> None:
        raise AssertionError(f"Habr click solver must click in the frame, not the page ({selector})")

    async def submit_status(self, *, timeout_s: float) -> int | None:
        del timeout_s
        return None

    async def current_url(self) -> str:
        return self.visited[-1] if self.visited else ""

    async def wait_for_url(self, pattern: str, *, timeout_s: float) -> bool:
        del pattern, timeout_s
        return False

    async def visible(self, selector: str) -> bool:
        return self.advanced_visible and "advanced" in selector

    async def click_in_frame(self, frame_selector: str, selector: str, *, timeout_s: float = 15.0) -> None:
        del timeout_s
        self.frame_clicks.append((frame_selector, selector))

    async def wait_for_value(self, selector: str, *, timeout_s: float) -> str | None:
        del timeout_s
        self.value_selectors.append(selector)
        if self.token is None or len(self.frame_clicks) < self.token_after_clicks:
            return None
        return self.token

    async def wait_for_hidden(self, selector: str, *, timeout_s: float) -> bool:
        del timeout_s
        self.hidden_selectors.append(selector)
        return self.spinner_hidden


class FakeHabrBrowserDriver:
    """One-shot scripted driver: opens ``session`` once, or fails when there is none."""

    def __init__(self, session: FakeHabrBrowserSession | None = None, *, engine_error: str | None = None) -> None:
        self._session = session
        self._engine_error = engine_error
        self.sessions: list[FakeHabrBrowserSession] = []
        self.opens = 0
        self.ensure_engine_calls = 0
        self.events: list[str] = []

    async def ensure_engine(self) -> Result[None, str]:
        self.ensure_engine_calls += 1
        self.events.append("ensure_engine")
        if self._engine_error is not None:
            return Err(self._engine_error)
        return Ok(None)

    def session(self, *, headless: bool) -> AbstractAsyncContextManager[BrowserSession]:
        del headless
        self.events.append("session")

        @asynccontextmanager
        async def _open() -> AsyncGenerator[FakeHabrBrowserSession]:
            self.opens += 1
            if self._session is None:
                raise RuntimeError("no browser session configured")
            self.sessions.append(self._session)
            yield self._session

        return _open()
