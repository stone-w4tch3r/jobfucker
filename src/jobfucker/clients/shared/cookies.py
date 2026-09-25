"""Board-neutral persisted cookie value type and domain filtering.

A client that authenticates with website cookies persists a portable subset of
its cookie jar (name/value/domain/path/secure/expires) and restores it on the
next run. :class:`PersistedCookie` is the shared value type; the per-board
domain allow-list predicate is supplied by the board (see
``TransportConfig.cookie_domain_allowed``) so tracker/analytics cookies never
leak into persisted state.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field


class PersistedCookie(BaseModel):
    """Portable subset of a website cookie (safe to persist and re-import)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    value: str
    domain: str = Field(min_length=1)
    path: str = "/"
    secure: bool = True
    expires: int | None = None


# Per-board predicate deciding whether a cookie's domain belongs to the board
# family (trackers excluded) so only session cookies are persisted.
CookieDomainPredicate = Callable[[str], bool]
