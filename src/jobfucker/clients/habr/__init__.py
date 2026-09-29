"""HTTP-only Habr Career client package.

Exports the configuration surface plus the concrete ``Client`` implementation
(``jobfucker.clients.habr.client.HabrClient``) registered under the ``habr``
service.
"""

from jobfucker.clients.habr.client import HabrClient
from jobfucker.clients.habr.config import (
    HabrFilterConfig,
    HabrSearchEntry,
    HabrSearchType,
    HabrServiceConfig,
)

__all__ = ["HabrClient", "HabrFilterConfig", "HabrSearchEntry", "HabrSearchType", "HabrServiceConfig"]
