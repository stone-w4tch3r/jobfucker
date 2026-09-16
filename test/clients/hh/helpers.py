"""Shared HH BDD scaffolding (test/AGENTS.md §3b, §8).

Each ``test/clients/hh/test_*_bdd.py`` collector keeps its own transports and
frozen setup/outcome carriers; this module holds only the scaffolding those
files used to hand-write seven times: the auth seam, auth-state seeding, the
canned JSON payload types + builders, the recording captcha solver, scripted
response playback, and the client factory. Transports are dumb routers over
data configured by ``Given`` steps — never scenario-kind enums.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import NotRequired, TypedDict

import httpx
from rusty_results.prelude import Ok, Result

from jobfucker.clients.base import (
    AuthInteractionProvider,
    CaptchaHandler,
    ClientCredentials,
    ClientDeps,
)
from jobfucker.clients.hh.browser import BrowserDriver
from jobfucker.clients.hh.client import HHClient
from jobfucker.clients.hh.config import HHServiceConfig
from jobfucker.clients.hh.models import PersistedAuthState, PersistedCookie
from test.clients.hh.browser_fake import FakeBrowserDriver

DEFAULT_LOGIN = "person@example.test"
DEFAULT_PASSWORD = "test-password"
DEFAULT_EXPIRES_AT = 4_000_000_000.0
DEFAULT_REFRESH_TOKEN = "refresh-restored"

PNG_IMAGE: bytes = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

JsonBlob = dict[
    str, object
]  # lint-ignore[restricted-object,raw-dict]: test fixture JSON blob  # lint-ignore[raw-dict]: test JSON


class StubAuthInteraction:
    """Non-interactive auth seam; persisted auth means it is normally unused."""

    def __init__(
        self,
        *,
        code: Result[str, str] | None = None,
        confirm: Result[bool, str] | None = None,
    ) -> None:
        default_code: Result[str, str] = Ok("123456")
        default_confirmed: bool = True
        default_confirm: Result[bool, str] = Ok(default_confirmed)
        self._code = code if code is not None else default_code
        self._confirm = confirm if confirm is not None else default_confirm

    async def request_code(self, *, prompt: str) -> Result[str, str]:
        del prompt
        return self._code

    async def confirm(self, *, prompt: str) -> Result[bool, str]:
        del prompt
        return self._confirm


def seed_auth_state(
    data_dir: Path,
    profile_id: str,
    *,
    access_token: str,
    refresh_token: str = DEFAULT_REFRESH_TOKEN,
    expires_at: float = DEFAULT_EXPIRES_AT,
    cookies: tuple[PersistedCookie, ...] = (),
) -> Path:
    """Persist a valid external auth-state file; return its stable path."""
    state_path = data_dir / "hh" / profile_id / "auth-state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        PersistedAuthState(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            cookies=cookies,
        ).model_dump_json(),
        encoding="utf-8",
    )
    return state_path


# --- Canned JSON payload types (shape of the documented HH wire bodies) ------


class EmployerPayload(TypedDict):
    name: str


class SkillPayload(TypedDict):
    name: str


class TestPayload(TypedDict):
    required: bool


class CatalogItemPayload(TypedDict):
    """A short catalog/listing item: id + display name + web url."""

    id: str
    name: str
    alternate_url: str


class ResumeStatusPayload(TypedDict):
    id: str
    name: str


class ResumeItemPayload(TypedDict):
    id: str
    title: str
    updated_at: str
    status: ResumeStatusPayload
    can_publish_or_update: NotRequired[bool | None]


class ResumesPagePayload(TypedDict):
    items: list[ResumeItemPayload]
    page: int
    pages: int
    per_page: int


class DetailPayload(TypedDict):
    """The canned vacancy-detail JSON body."""

    id: str
    name: str
    alternate_url: str
    employer: EmployerPayload
    description: str
    key_skills: list[SkillPayload]
    salary: NotRequired[JsonBlob | None]
    archived: NotRequired[bool]
    has_test: NotRequired[bool]
    test: NotRequired[TestPayload]
    response_url: NotRequired[str]


class ErrorItemPayload(TypedDict):
    value: str
    type: str


class EnvelopePayload(TypedDict):
    """The canned error-envelope JSON body."""

    errors: list[ErrorItemPayload]
    description: NotRequired[str]
    bad_argument: NotRequired[str]


# --- Canned response builders ------------------------------------------------


def json_response(payload: object, status: int = 200) -> httpx.Response:  # lint-ignore[restricted-object]: test JSON
    return httpx.Response(status, headers={"content-type": "application/json"}, json=payload)


def applicant_healthcheck() -> httpx.Response:
    return json_response({"id": "applicant-1", "auth_type": "applicant", "is_applicant": True})


def resume_item(
    item_id: str,
    *,
    title: str | None = None,
    status: str = "published",
    updated_at: str = "2026-08-30T10:00:00+0300",
    can_publish: bool | None = None,
) -> ResumeItemPayload:
    return {
        "id": item_id,
        "title": title if title is not None else f"Резюме {item_id}",
        "updated_at": updated_at,
        "status": {"id": status, "name": status},
        "can_publish_or_update": can_publish,
    }


def resumes_page(items: list[ResumeItemPayload], page: int = 0, *, pages: int = 0) -> ResumesPagePayload:
    return {"items": items, "page": page, "pages": pages, "per_page": 100}


def vacancy_detail(
    vacancy_id: str,
    *,
    name: str = "Python разработчик",
    employer: str = "Компания",
    description: str = "<p>Описание</p>",
    skills: tuple[str, ...] = ("Python",),
    archived: bool = False,
    has_test: bool = False,
    response_url: str | None = None,
    salary: JsonBlob | None = None,
) -> DetailPayload:
    """A canned ``GET /vacancies/{id}`` body with the scenario's overrides."""
    detail = DetailPayload(
        id=vacancy_id,
        name=name,
        alternate_url=f"https://hh.ru/vacancy/{vacancy_id}",
        employer=EmployerPayload(name=employer),
        description=description,
        key_skills=[SkillPayload(name=skill) for skill in skills],
        salary=salary,
    )
    if archived:
        detail["archived"] = True
    if has_test:
        detail["has_test"] = True
        detail["test"] = TestPayload(required=True)
    if response_url is not None:
        detail["response_url"] = response_url
    return detail


class RecordingSolver:
    """``CaptchaHandler`` double: records served images, replays scripted answers.

    Answers pop in order; the last one repeats when the script runs out.
    """

    def __init__(self, *answers: Result[str, str]) -> None:
        default_answers: tuple[Result[str, str], ...] = (Ok("answer"),)
        self._answers = answers if answers else default_answers
        self.images: list[bytes] = []

    async def __call__(self, image: bytes) -> Result[str, str]:
        self.images.append(image)
        index = min(len(self.images) - 1, len(self._answers) - 1)
        return self._answers[index]


class ScriptedResponses:
    """Pop scripted responses in order; the last one repeats when exhausted.

    Exception entries are raised (transport-level faults such as
    ``httpx.ConnectError``).
    """

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        if not responses:
            raise ValueError("scripted responses must not be empty")
        self._responses = responses
        self._calls = 0

    def next(self) -> httpx.Response:
        response = self._responses[min(self._calls, len(self._responses) - 1)]
        self._calls += 1
        if isinstance(response, Exception):
            raise response
        return response


# --- Client factory ----------------------------------------------------------


def hh_client(
    *,
    data_dir: Path,
    profile_id: str,
    config: HHServiceConfig,
    transport: httpx.AsyncBaseTransport,
    solver: CaptchaHandler,
    auth_interaction: AuthInteractionProvider | None = None,
    browser_driver: BrowserDriver | None = None,
    apply_delay: Callable[[], Awaitable[None]] | None = None,
    login: str = DEFAULT_LOGIN,
    password: str = DEFAULT_PASSWORD,
) -> HHClient:
    """Construct an HHClient over the scenario's fakes.

    ``browser_driver`` defaults to the scripted fake, never the real
    :class:`PatchrightDriver`: the engine startup preflight must not spawn a
    node driver (or auto-download an engine) inside BDD scenarios.
    """
    deps = ClientDeps(
        service="hh",
        profile_id=profile_id,
        data_dir=data_dir,
        credentials=ClientCredentials(login=login, password=password),
        auth_interaction=auth_interaction if auth_interaction is not None else StubAuthInteraction(),
        captcha_handler=solver,
    )
    return HHClient(
        deps,
        config,
        http_transport=transport,
        browser_driver=browser_driver if browser_driver is not None else FakeBrowserDriver([]),
        apply_delay=apply_delay,
    )
