"""Board-neutral client mechanics shared by every board implementation.

Everything here is dependency-light and board-free: HTTP transport/session
ownership, atomic JSON persistence, persisted cookies, HTML-to-text parsing, and
the browser automation seam. Board packages under ``clients/<board>/`` compose
these; the core (engine, stages, storage, config) never imports them. No module
in this package may import a board package.
"""

from __future__ import annotations
