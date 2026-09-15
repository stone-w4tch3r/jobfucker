"""Service-name → client registry (contract §6 / §6.1).

The factory is the **only** place a config ``service`` string maps to a concrete
client; the core never hardcodes a board. It registers a client class,
and binds the run's ``service.<board>`` config section at construction, so each
client self-configures from its own typed section. ``get`` and ``cap`` raise
(fail-fast) on an unknown service — that is a programmer/config error, never a
``Result``.

The registry is bound to a :class:`ClientDeps` (built by the composition root
once) and the run's config section at construction, because every client is
constructed from the same ``(deps, section)`` pair (contract §7.2); the config
section arrives here, not through the engine.
"""

from __future__ import annotations

from collections.abc import Callable

from jobfucker.clients.base import Client, ClientDeps, ServiceConfigSection


class Factory:
    """Read-only registry keyed by the config ``service`` value."""

    def __init__(self, deps: ClientDeps, section: ServiceConfigSection | None = None) -> None:
        """Bind the factory to the deps and config section every client receives.

        Args:
            deps: the :class:`ClientDeps` (profile, data_dir, interaction,
                captcha handler) the composition root builds once.
            section: the validated ``service.<board>`` config section for the
                run's board; optional so a config-less factory can exist, but
                ``get`` fails fast if no section is bound.
        """
        self._deps: ClientDeps = deps
        self._section: ServiceConfigSection | None = section
        self._registry: dict[str, Callable[[ClientDeps, ServiceConfigSection], Client]] = {}

    def register(self, service: str, client: type[Client]) -> None:
        """Register a client class under ``service``.

        Args:
            service: the config ``service`` value this client answers to.
            client: the client class; constructed from ``self._deps`` and
                ``self._section`` on ``get``.
        """
        # Erasure point: the registry is heterogeneous (each client is a distinct
        # concrete class) and stores each class as a plain callable of
        # ``(deps, section) -> Client``. A concrete client class is not statically
        # assignable to that callable (its own service_info class attribute, and any
        # narrower constructor shape, are erased here), so the widening is suppressed
        # deliberately.
        self._registry[service] = client  # type: ignore[assignment]  # rationale: type erasure at a heterogeneous registry boundary (distinct concrete client per service, stored as a plain callable)

    def get(self, service: str) -> Client:
        """Return a constructed client for ``service`` (fail-fast if unknown).

        Args:
            service: the config ``service`` value.

        Returns:
            A newly constructed client built from ``self._deps`` and
            ``self._section``.

        Raises:
            KeyError: when ``service`` was never registered.
            RuntimeError: when the factory has no bound config section.
        """
        if self._section is None:
            raise RuntimeError("Factory has no bound config section; cannot construct a client")
        try:
            client = self._registry[service]
        except KeyError:
            registered = ", ".join(sorted(self._registry)) or "<none>"
            raise KeyError(f"Unknown service {service!r}; registered services: {registered}") from None
        return client(self._deps, self._section)

    def cap(self, service: str) -> int:
        """Return ``service``'s per-auth daily cap without constructing a client.

        Args:
            service: the config ``service`` value.

        Returns:
            The client class's class-level ``per_auth_daily_cap``.

        Raises:
            KeyError: when ``service`` was never registered.
        """
        return self._client(service).service_info.per_auth_daily_cap

    def max_search_items(self, service: str) -> int | None:
        """Return ``service``'s searchable-listing cap without constructing a client.

        ``None`` when the client declares no cap (e.g. the mock).

        Raises:
            KeyError: when ``service`` was never registered.
        """
        return self._client(service).service_info.max_search_items

    def _client(self, service: str) -> type[Client]:
        """The registered client class for ``service`` (fail-fast if unknown)."""
        try:
            client = self._registry[service]
        except KeyError:
            registered = ", ".join(sorted(self._registry)) or "<none>"
            raise KeyError(f"Unknown service {service!r}; registered services: {registered}") from None
        # Same registry erasure as ``register``: the stored callable hides the
        # concrete class attributes (``service_info``) every client carries.
        return client  # type: ignore[return-value]  # rationale: type erasure at a heterogeneous registry boundary (see register)
