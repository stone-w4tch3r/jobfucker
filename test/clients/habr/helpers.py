"""Shared Habr BDD scaffolding (test/AGENTS.md §3b, §8).

Each ``test/clients/habr/test_*_bdd.py`` collector keeps its own frozen
setup/outcome carriers and its own router wiring; this module holds only the
reusable scaffolding: the auth seam, a scripted response player, the routed
mock transport, the canned Habr payload/image builders, the recording captcha
handler, and the coordinator/deps factories.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

import httpx
from rusty_results.prelude import Ok, Result

from jobfucker.clients.base import (
    AuthInteractionProvider,
    CaptchaHandler,
    ClientCredentials,
    ClientDeps,
)
from jobfucker.clients.habr.captcha import BrowserClickSolver, HttpVisionSolver, LoginCaptcha
from jobfucker.clients.habr.models import PersistedHabrIdentity, PersistedHabrSession
from jobfucker.clients.habr.transport import HabrTransport
from jobfucker.clients.shared.browser import BrowserDriver
from jobfucker.clients.shared.cookies import PersistedCookie
from test.clients.habr.browser_fake import FakeHabrBrowserDriver

DEFAULT_LOGIN: Final = "person@example.test"
DEFAULT_PASSWORD: Final = "test-password"
DEFAULT_ALIAS: Final = "habr-person"

# A 1x1 PNG (the shape the captcha handler receives, not a real challenge).
PNG_IMAGE: Final = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

JsonBlob = dict[str, object]
"""Untyped JSON blob for canned wire bodies (test-only)."""

RouteKey = tuple[str, str, str]
RouteBook = dict[RouteKey, list[httpx.Response]]


def profile_id_for(login: str = DEFAULT_LOGIN) -> str:
    """Stable profile id derived from the login (the composition-root convention)."""
    return login.strip().casefold().replace("@", "-").replace(".", "-")


class StubAuthInteraction:
    """Non-interactive auth seam; the Habr flow never prompts."""

    async def request_code(self, *, prompt: str) -> Result[str, str]:
        del prompt
        return Ok("000000")

    async def confirm(self, *, prompt: str) -> Result[bool, str]:
        del prompt
        confirmed: bool = True
        return Ok(confirmed)


class RecordingSolver:
    """``CaptchaHandler`` double: records served images, replays scripted answers."""

    def __init__(self, *answers: Result[str, str]) -> None:
        defaults: tuple[Result[str, str], ...] = (Ok("answer"),)
        self._answers = answers if answers else defaults
        self.images: list[bytes] = []

    async def __call__(self, image: bytes) -> Result[str, str]:
        self.images.append(image)
        index = min(len(self.images) - 1, len(self._answers) - 1)
        return self._answers[index]


class ScriptedResponses:
    """Pop scripted responses in order; the last one repeats when exhausted.

    An entry may be an exception instance: it is raised on the matching call so a
    post-send transport fault can be scripted (mirrors the HH helper).
    """

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        if not responses:
            raise ValueError("scripted responses must not be empty")
        self._responses = responses
        self._calls = 0
        self._last = responses[-1]

    def next(self) -> httpx.Response:
        entry = self._responses[min(self._calls, len(self._responses) - 1)]
        self._calls += 1
        if isinstance(entry, Exception):
            raise entry
        return entry

    @property
    def calls(self) -> int:
        return self._calls


class RoutedTransport:
    """Dumb ``httpx.MockTransport`` router: exact routes + a recorded request history.

    Each route holds an ordered response script; the last response repeats once
    the script is exhausted, so a repeated endpoint (e.g. a CSRF scrape twice)
    can be asserted without an unbounded route list.
    """

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self._routes: RouteBook = {}

    def on(self, method: str, host: str, path: str, *responses: httpx.Response) -> None:
        if not responses:
            raise ValueError("a route needs at least one response")
        self._routes[(method.upper(), host, path)] = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        book = self._routes.get((request.method, request.url.host, request.url.path))
        if book is None:
            raise AssertionError(f"Unexpected Habr request: {request.method} {request.url}")
        if len(book) == 1:
            return book[0]
        return book.pop(0)


def json_response(payload: object, status: int = 200) -> httpx.Response:  # lint-ignore[restricted-object]: test JSON
    return httpx.Response(status, headers={"content-type": "application/json"}, json=payload)


def html_response(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "text/html"}, text=body)


def png_response() -> httpx.Response:
    return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG_IMAGE)


# --- Canned Habr payloads ----------------------------------------------------


def authorized_identity(
    alias: str = DEFAULT_ALIAS,
    *,
    full_name: str = "Хабр Человек",
    career_session: str | None = None,
) -> httpx.Response:
    response = json_response({"user": {"alias": alias, "fullName": full_name, "email": f"{alias}@example.test"}})
    if career_session is not None:
        response.headers["set-cookie"] = (
            f"_career_session={career_session}; Domain=career.habr.com; Path=/; Secure; HttpOnly"
        )
    return response


def anonymous_identity() -> httpx.Response:
    return json_response({})


def csrf_page_html(token: str, *, content_first: bool = False) -> str:
    """An HTML page carrying the Rails CSRF meta tag in either attribute order."""
    if content_first:
        return f'<html><head><meta content="{token}" name="csrf-token"></head><body>ok</body></html>'
    return f'<html><head><meta name="csrf-token" content="{token}"></head><body>ok</body></html>'


def login_page_html(*, action: str, sitekey: str) -> str:
    return (
        "<html><body>"
        f'<form action="{action}" method="post" id="ident-form">'
        '<input type="email" name="email">'
        '<input type="password" name="password">'
        '<input type="hidden" name="smart-token" value=""></form>'
        f'<div class="smart-captcha" data-captcha="yandex" data-sitekey="{sitekey}"></div>'
        "</body></html>"
    )


def login_success(rurl: str) -> httpx.Response:
    return json_response({"success": True, "rurl": rurl})


def login_failure(message: str = "invalid credentials") -> httpx.Response:
    return json_response({"success": False, "error": message})


# --- SmartCaptcha ladder payloads -------------------------------------------


def _b64url(value: str) -> str:
    return base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")


def image_loader(image_url: str) -> str:
    """Wrap a real image URL the way the widget does (the decode-ladder input)."""
    return f"https://smartcaptcha.yandexcloud.net/load-captchaimg?{_b64url(image_url)},rest"


def pow_payload(complexity: int = 10) -> JsonBlob:
    prefix = b"t=1;p=probe;c=10;d=0123456789abcdef;"
    return {"prefix": prefix.hex(), "complexity": complexity}


def checkbox_challenge(key: str, *, complexity: int = 10) -> httpx.Response:
    return json_response(
        {"status": "failed", "captcha": {"type": "checkbox", "key": key, "image": None}, "pow": pow_payload(complexity)}
    )


def image_challenge(key: str, image_url: str, *, complexity: int = 10) -> httpx.Response:
    return json_response(
        {
            "status": "failed",
            "captcha": {"type": "image", "key": key, "image": image_loader(image_url)},
            "pow": pow_payload(complexity),
        }
    )


def captcha_passed(spravka: str = "spravka-token") -> httpx.Response:
    return json_response({"status": "ok", "spravka": spravka})


def unknown_challenge(key: str) -> httpx.Response:
    return json_response({"status": "failed", "captcha": {"type": "voice", "key": key, "image": None}})


# --- Factories ---------------------------------------------------------------


def habr_deps(
    data_dir: Path,
    profile_id: str,
    solver: CaptchaHandler,
    *,
    login: str = DEFAULT_LOGIN,
    password: str = DEFAULT_PASSWORD,
    auth_interaction: AuthInteractionProvider | None = None,
) -> ClientDeps:
    return ClientDeps(
        service="habr",
        profile_id=profile_id,
        data_dir=data_dir,
        credentials=ClientCredentials(login=login, password=password),
        auth_interaction=auth_interaction if auth_interaction is not None else StubAuthInteraction(),
        captcha_handler=solver,
    )


def habr_transport(handler: Callable[[httpx.Request], httpx.Response]) -> HabrTransport:
    """Wrap any request handler (a routed fake or the auth router) as a HabrTransport."""
    return HabrTransport(transport=httpx.MockTransport(handler))


def habr_captcha(
    transport: HabrTransport,
    solver: CaptchaHandler,
    *,
    driver: BrowserDriver | None = None,
    max_attempts: int = 4,
) -> LoginCaptcha:
    browser_solver = BrowserClickSolver(
        driver if driver is not None else FakeHabrBrowserDriver(), max_attempts=max_attempts
    )
    vision_solver = HttpVisionSolver(transport, solver, max_attempts=max_attempts)
    return LoginCaptcha(browser_solver=browser_solver, vision_solver=vision_solver)


def seed_habr_session(
    data_dir: Path,
    profile_id: str,
    *,
    cookies: tuple[PersistedCookie, ...] = (),
    identity: PersistedHabrIdentity | None = None,
) -> Path:
    """Persist a session snapshot; return its stable path."""
    session_path = data_dir / "habr" / profile_id / "session.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    session = PersistedHabrSession(cookies=cookies, identity=identity)
    session_path.write_text(session.model_dump_json(), encoding="utf-8")
    return session_path


def session_path(data_dir: Path, profile_id: str) -> Path:
    return data_dir / "habr" / profile_id / "session.json"


# --- Listing / detail payloads -----------------------------------------------

_DEFAULT_PUBLISHED: Final = "2026-09-15T10:23:50+03:00"


def listing_item(
    item_id: int,
    *,
    title: str = "Python разработчик",
    href: str | None = None,
    company_title: str | None = "Компания",
    salary: JsonBlob | None = None,
    predicted_salary: JsonBlob | None = None,
    locations: tuple[str, ...] | None = ("Москва",),
    skills: tuple[str, ...] | None = (),
    published_date: str | None = _DEFAULT_PUBLISHED,
    archived: bool = False,
    hidden: bool = False,
    response_kind: str = "direct",
) -> JsonBlob:
    """One ``list[]`` element in the shape the Habr listing returns.

    ``company_title=None`` emits a null company (concealed/absent). ``salary`` is
    the stated salary; ``predicted_salary`` is the separate board prediction,
    included only to prove it is never merged into ``salary``. ``locations``/
    ``skills`` set to ``None`` emit the live-observed JSON ``null`` list.
    """
    item: JsonBlob = {
        "id": item_id,
        "href": href if href is not None else f"/vacancies/{item_id}",
        "title": title,
        "company": None if company_title is None else {"title": company_title},
        "salary": salary,
        "locations": None if locations is None else [{"title": location} for location in locations],
        "skills": None if skills is None else [{"title": skill} for skill in skills],
        "archived": archived,
        "hidden": hidden,
        "response": {"kind": response_kind},
    }
    if published_date is not None:
        item["publishedDate"] = {"date": published_date}
    if predicted_salary is not None:
        item["predictedSalary"] = predicted_salary
    return item


def listing_page(
    items: Sequence[JsonBlob],
    *,
    total_results: int | None = None,
    current_page: int = 1,
    per_page: int = 50,
    total_pages: int | None = None,
) -> httpx.Response:
    """A ``200`` listing envelope over ``items`` with consistent ``meta``."""
    found = total_results if total_results is not None else len(items)
    pages = total_pages if total_pages is not None else (1 if not items else -(-found // max(per_page, 1)))
    return json_response(
        {
            "list": list(items),
            "meta": {
                "totalResults": found,
                "perPage": per_page,
                "currentPage": current_page,
                "totalPages": pages,
            },
        }
    )


def ssr_detail_page(
    item_id: int,
    *,
    title: str = "Python разработчик",
    href: str | None = None,
    company_title: str | None = "Компания",
    description: str = "<p>Описание</p>",
    skills: tuple[str, ...] | None = (),
    salary: JsonBlob | None = None,
    predicted_salary: JsonBlob | None = None,
    locations: tuple[str, ...] | None = ("Москва",),
    published_date: str | None = _DEFAULT_PUBLISHED,
    archived: bool = False,
    hidden: bool = False,
    response_kind: str = "direct",
    reordered_script: bool = False,
) -> str:
    """A detail page whose inline SSR block carries the vacancy object.

    The JSON is written with Habr's ``\\u003c``-style escaping so the decode path
    (which unescapes) is genuinely exercised. ``reordered_script`` emits the
    state attributes in a different order plus an extra attribute, proving the
    extraction anchors on the attributes rather than their order.
    """
    vacancy = listing_item(
        item_id,
        title=title,
        href=href,
        company_title=company_title,
        salary=salary,
        predicted_salary=predicted_salary,
        locations=locations,
        skills=skills,
        published_date=published_date,
        archived=archived,
        hidden=hidden,
        response_kind=response_kind,
    )
    vacancy["description"] = description
    blob = json.dumps({"vacancy": vacancy}, ensure_ascii=False)
    escaped = blob.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    tag = (
        '<script nonce="abc123" data-ssr-state="true" type="application/json">'
        if reordered_script
        else '<script type="application/json" data-ssr-state="true">'
    )
    return f"<html><head></head><body>{tag}{escaped}</script></body></html>"


def ssr_page_with_mistyped_script(item_id: int) -> str:
    """A detail page whose ``data-ssr-state`` block is not an ``application/json`` script.

    The anchor is the attribute, but the type must still be confirmed: a state
    block re-typed (or untyped) must not be parsed as JSON.
    """
    vacancy = listing_item(item_id)
    blob = json.dumps({"vacancy": vacancy}, ensure_ascii=False)
    return f'<html><body><script data-ssr-state="true">{blob}</script></body></html>'


class RecordingApplyDelay:
    """Recording no-op ``ApplyDelay``: counts invocations without sleeping.

    ``observer`` runs at the start of each pacing call, so a scenario can
    snapshot state (e.g. the request history) at the moment the wait began —
    the only way to prove a prefetch happened *before* the pacing, since the
    recorded request order alone cannot distinguish the two.
    """

    def __init__(self, observer: Callable[[], None] | None = None) -> None:
        self.calls = 0
        self._observer = observer

    async def __call__(self) -> None:
        if self._observer is not None:
            self._observer()
        self.calls += 1
