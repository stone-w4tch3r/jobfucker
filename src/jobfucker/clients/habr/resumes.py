"""Single-resume mapping for a Habr account.

Habr has no owned-resume JSON endpoint and no numeric resume id: the account
resume is the public profile page, and its identifier is the account
``alias``. One account therefore exposes exactly one :class:`ResumeInfo`, whose
``updated_at`` is not established by the board (``None``), and applying carries
no resume parameter (docs/specs/habr-client.md §Resumes).
"""

from __future__ import annotations

from rusty_results.prelude import Ok, Result

from jobfucker.clients.base import ClientError, ResumeInfo, ServiceIdentity

__all__ = ["ResumeService"]


class ResumeService:
    """Map the authorized identity onto Habr's single-resume contract."""

    async def list_resumes(self, identity: ServiceIdentity) -> Result[list[ResumeInfo], ClientError]:
        """Return the one resume the account owns (``resume_id`` = account alias)."""
        title = identity.display_name if identity.display_name else identity.external_id
        return Ok([ResumeInfo(resume_id=identity.external_id, title=title, updated_at=None)])
