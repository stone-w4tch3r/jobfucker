"""Board registry: the one place a config ``service`` value maps to concrete code.

Every first-party board appears here exactly twice: as its ``service.<board>``
section model in :data:`SERVICE_SECTION_MODELS` (the registry the config loader
validates against) and as its registered ``Client`` class in
:func:`register_clients` (the factory the composition roots build). Keeping both
in one module means adding a board touches no core module — ``config.py`` and
``bootstrap.py`` import this registry instead of a concrete board.

This module is the single permitted core-side entry point into board packages;
``clients/shared/`` stays board-free and boards never import ``jobfucker.config``.
"""

from __future__ import annotations

from pydantic import BaseModel

from jobfucker.clients.factory import Factory
from jobfucker.clients.habr.client import HabrClient
from jobfucker.clients.habr.config import HabrServiceConfig
from jobfucker.clients.hh.client import HHClient
from jobfucker.clients.hh.config import HHServiceConfig
from jobfucker.clients.mock.client import MockClient
from jobfucker.clients.mock.params import MockServiceConfig

__all__ = ["SERVICE_SECTION_MODELS", "SectionModel", "register_clients"]

# A board's ``service.<board>`` section is a strict pydantic model implementing
# the contract's ``ServiceConfigSection`` protocol.
SectionModel = type[BaseModel]

# Board name -> concrete ``service.<board>`` section model (the available
# first-party clients). Immutable by convention (module-level dict; enforced by
# the custom linter marker below).
SERVICE_SECTION_MODELS: dict[str, SectionModel] = {  # lint-ignore[module-mutable-state]: m  # lint-ignore[raw-dict]: m
    "habr": HabrServiceConfig,
    "hh": HHServiceConfig,
    "mock": MockServiceConfig,
}


def register_clients(factory: Factory) -> None:
    """Register every first-party client class on ``factory``.

    Args:
        factory: the composition root's :class:`Factory`, already bound to the
            run's deps and config section.
    """
    factory.register("habr", HabrClient)
    factory.register("hh", HHClient)
    factory.register("mock", MockClient)
