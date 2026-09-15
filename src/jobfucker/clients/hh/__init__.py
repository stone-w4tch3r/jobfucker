"""HTTP-only HH.ru client."""

from jobfucker.clients.hh.client import HHClient
from jobfucker.clients.hh.config import HHFilterConfig, HHSearchFilters, HHSearchMode, HHServiceConfig

__all__ = ["HHClient", "HHFilterConfig", "HHSearchFilters", "HHSearchMode", "HHServiceConfig"]
