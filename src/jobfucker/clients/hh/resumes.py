"""HH owned-resume listing and application validation boundary."""

from __future__ import annotations

from typing import Final

from pydantic import ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import ClientError, ConfigurationError, ProtocolError
from jobfucker.clients.hh.apireads import get_api_with_recovery, status_error
from jobfucker.clients.hh.captcha import CaptchaCoordinator
from jobfucker.clients.hh.models import ResumeListItem, ResumesPage
from jobfucker.clients.hh.transport import FormFields, HHTransport

_RESUMES_PAGE_SIZE: Final = 100
# Resume counts are tiny (single digits for a real account); the cap bounds a
# buggy/hostile upstream that keeps growing `pages` (10 pages x 100 items).
_MAX_RESUME_PAGES: Final = 10
_PUBLISHED_STATUS: Final = "published"


class ResumeService:
    """Owned-resume listing and configured-resume validation for one HH session."""

    def __init__(self, transport: HHTransport, captcha: CaptchaCoordinator) -> None:
        self._transport = transport
        self._captcha = captcha

    async def list_resumes(self, access_token: str) -> Result[list[ResumeListItem], ClientError]:
        """Page through GET /resumes/mine and collect every owned resume.

        An account without resumes yields ``Ok([])`` (the wire answer is a
        page with zero items, not an error). ``pages`` is the total page
        count, so the walk covers pages ``0..pages-1`` — requesting a page
        past the end returns clamped/duplicate content (live-verified).
        """
        resumes: list[ResumeListItem] = []
        page = 0
        while True:
            page_result = await self._read_page(access_token, page)
            if page_result.is_err:
                return Err(page_result.unwrap_err())
            resumes_page = page_result.unwrap()
            if resumes_page.pages > _MAX_RESUME_PAGES:
                # Failing loudly beats a silently truncated listing (and a
                # false "not owned" verdict in validate_resume).
                return Err(
                    ProtocolError(
                        message=(
                            f"HH reports {resumes_page.pages} resume pages; "
                            f"aborting above the {_MAX_RESUME_PAGES}-page bound"
                        )
                    )
                )
            resumes.extend(resumes_page.items)
            if page + 1 >= resumes_page.pages:
                return Ok(resumes)
            page += 1

    async def validate_resume(self, access_token: str, resume_id: str) -> Result[ResumeListItem, ClientError]:
        """Verify the configured resume is owned by this account and published.

        ``GET /resumes/mine`` returns only owned resumes, so presence proves
        ownership. A missing or unpublished configured resume is a
        configuration error that must stop the pipeline (not per-vacancy).
        The ``pages`` walk mirrors :meth:`list_resumes` (total page count).
        """
        page = 0
        while True:
            page_result = await self._read_page(access_token, page)
            if page_result.is_err:
                return Err(page_result.unwrap_err())
            resumes_page = page_result.unwrap()
            if resumes_page.pages > _MAX_RESUME_PAGES:
                # A legitimate huge account must not be misreported as an
                # unowned configured resume; abort loudly instead.
                return Err(
                    ProtocolError(
                        message=(
                            f"HH reports {resumes_page.pages} resume pages; "
                            f"aborting above the {_MAX_RESUME_PAGES}-page bound"
                        )
                    )
                )
            for item in resumes_page.items:
                if item.id == resume_id:
                    if item.status.id != _PUBLISHED_STATUS:
                        return Err(
                            ConfigurationError(
                                message=(f"HH resume {resume_id} is not published (status: {item.status.id})")
                            )
                        )
                    return Ok(item)
            if page + 1 >= resumes_page.pages:
                return Err(ConfigurationError(message=f"HH resume {resume_id} is not owned by this account"))
            page += 1

    async def _read_page(self, access_token: str, page: int) -> Result[ResumesPage, ClientError]:
        """Fetch and decode one native /resumes/mine page with challenge recovery."""
        params: FormFields = (("page", str(page)), ("per_page", str(_RESUMES_PAGE_SIZE)))
        response_result = await get_api_with_recovery(
            self._transport, self._captcha, "/resumes/mine", access_token, params=params
        )
        if response_result.is_err:
            return Err(response_result.unwrap_err())
        response = response_result.unwrap()
        status_failure = status_error(response, operation="resume listing")
        if status_failure is not None:
            return Err(status_failure)
        try:
            return Ok(ResumesPage.model_validate_json(response.content))
        except ValidationError:
            return Err(ProtocolError(message="Malformed HH resume listing response", status=response.status_code))
