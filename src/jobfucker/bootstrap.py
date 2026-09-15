"""CLI engine-build composition facade.

This module is the **UI-specific** slice of the composition graph: everything the
CLI needs to actually run a stage — selecting a terminal captcha handler,
building the :class:`Factory` bound to the run's config section, and
reconstructing a runnable :class:`Engine` from a stored pipeline row. It is
intentionally **not** the shared graph: the UI-neutral composition root (storage
+ factory + pipeline service, no terminal concepts) lives in
:mod:`jobfucker.app.services` (:class:`~jobfucker.app.services.AppServices`),
and both the CLI and the Qt GUI open that graph via ``AppServices.open()``.

This facade exists because ``build_engine_from_pipeline`` drives captcha
selection + AI construction that the engine-rebuild path requires and which are
inherently CLI/terminal concerns. It re-exports :class:`TerminalAuthInteraction`
so CLI-shaped callers/tests keep a single import target, and it keeps
:class:`~jobfucker.ai.OpenAIWrapper` importable in this namespace (the gated e2e
test patches ``jobfucker.bootstrap.OpenAIWrapper`` to stub the AI client without
any network access).

Engine-rebuild path (:func:`build_engine_from_pipeline`):

- marshals a stored :class:`Pipeline` row onto a
  :class:`~jobfucker.config.PersistedPipelineRefs` view via
  :func:`jobfucker.app.mapping.to_persisted_refs` (config↔storage mapping lives
  in the ``app/`` layer, not here);
- reconstructs a :class:`~jobfucker.config.PipelineConfig` via
  :func:`jobfucker.app.mapping.build_config_from_refs` — **zero file I/O** ("we
  trust the db"), so a run works even if the original referenced files were
  moved or deleted;
- selects the terminal captcha handler (fail-fast unless ``require_captcha=False``)
  and builds an :class:`Engine` from a :class:`Factory` that registers the
  available clients and binds the reconstructed run's config section.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from dataclasses import replace

import typer
from rusty_results.prelude import Err, Ok, Result
from typer import Abort

from jobfucker.ai import AiClient, OpenAIWrapper
from jobfucker.app.mapping import PipelineConfig, build_config_from_refs, to_persisted_refs
from jobfucker.captcha.selector import select_captcha_handler
from jobfucker.captcha.terminal import detect_terminal_protocol
from jobfucker.captcha.terminal_handlers import TerminalCaptchaHandler
from jobfucker.clients.base import CaptchaHandler, Client, ClientCredentials, ClientDeps, ServiceConfigSection
from jobfucker.clients.factory import Factory
from jobfucker.clients.hh.client import HHClient
from jobfucker.clients.mock.client import MockClient
from jobfucker.config import with_overrides
from jobfucker.engine import Engine
from jobfucker.reporting import NullReporter, Reporter
from jobfucker.runtime import data_dir
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, PipelineSnapshot

__all__ = [
    "TerminalAuthInteraction",
    "build_client_from_pipeline",
    "build_config_from_pipeline",
    "build_engine_from_pipeline",
]


def _prompt_text(text: str) -> str:
    """A ``str``-typed :func:`typer.prompt` (typer declares its return as ``Any``)."""
    return typer.prompt(text)  # type: ignore[reportAny]  # rationale: typer.prompt declares Any; one-line TTY read


class TerminalAuthInteraction:
    """A CLI :class:`AuthInteractionProvider`: typer-native terminal prompts.

    The client never reads the TTY directly; every interactive step goes
    through this provider, which prompts with :func:`typer.prompt` /
    :func:`typer.confirm` (no builtin ``input()``/``print()`` is used). A
    dismissed prompt — EOF or Ctrl+C, which typer collapses into
    :class:`Abort` — surfaces as ``Err`` (never a hang). Blocking reads run
    off the loop via ``asyncio.to_thread`` so a live event loop never freezes
    while a human types (async-migration D4).
    """

    async def request_code(self, *, prompt: str) -> Result[str, str]:
        """Ask for a one-time code, returning the entered value or an Err.

        ``typer.prompt`` re-asks on an empty line (so an empty answer can
        never slip through) and raises :class:`Abort` on EOF/Ctrl+C.
        """
        try:
            return Ok(await asyncio.to_thread(_prompt_text, prompt))
        except Abort:
            return Err("cancelled: no code provided (EOF)")

    async def confirm(self, *, prompt: str) -> Result[bool, str]:
        """Ask for a yes/no confirmation, returning the decision or an Err.

        ``typer.confirm`` renders ``[y/N]`` natively, interprets an empty
        answer as the default (no), and raises :class:`Abort` on EOF/Ctrl+C.
        """
        try:
            return Ok(await asyncio.to_thread(lambda: typer.confirm(prompt, default=False)))
        except Abort:
            return Err("cancelled: no confirmation (EOF)")


def _fallback_captcha_handler() -> CaptchaHandler:
    """A captcha handler for commands that never invoke a client.

    Used when no pipeline config is available or when ``require_captcha`` is
    false (AI-only stages); it is never actually called because no client is
    constructed, but :class:`ClientDeps` requires a callable.
    """
    detected = detect_terminal_protocol()
    if detected is not None:
        return TerminalCaptchaHandler(detected)

    async def _unavailable(_image: bytes) -> Result[str, str]:
        return Err("No captcha handler configured for this command")

    return _unavailable


def _build_factory(
    auth: TerminalAuthInteraction,
    handler: CaptchaHandler,
    service: str,
    credentials: ClientCredentials,
    section: ServiceConfigSection | None = None,
    reporter: Reporter | None = None,
) -> Factory:
    """Build the :class:`Factory` bound to a CLI :class:`ClientDeps`.

    The factory registers the available clients and binds the run's
    reconstructed ``service.<board>`` section, so a constructed client
    self-configures from it. ``reporter`` is the same live sink the
    :class:`Engine` gets — the client (and the shared paging driver) emit
    board-operation detail on it (contract §8).
    """
    deps = ClientDeps(
        service=service,
        profile_id=hashlib.sha256(credentials.login.strip().casefold().encode()).hexdigest(),
        data_dir=data_dir(),
        credentials=credentials,
        auth_interaction=auth,
        captcha_handler=handler,
        reporter=reporter if reporter is not None else NullReporter(),
    )
    factory = Factory(deps, section=section)
    factory.register("hh", HHClient)
    factory.register("mock", MockClient)
    return factory


def _rebuild_config_and_factory(
    pipeline: Pipeline,
    snapshot: PipelineSnapshot,
    *,
    use_sixel: bool = False,
    use_kitty: bool = False,
    no_captcha_ai: bool = False,
    require_captcha: bool = True,
    search_index: int = 0,
    query_override: str | None = None,
    filter_override: Mapping[str, object] | None = None,  # lint-ignore[restricted-object]: YAML payload boundary
    reporter: Reporter | None = None,
) -> Result[tuple[PipelineConfig, Factory], str]:
    """Rebuild the stored pipeline config and its client factory (shared prefix).

    Marshals the snapshot's CONTENT columns into a validated
    :class:`PipelineConfig` (zero file I/O — "we trust the db"), applies the
    optional search query/filter overrides to pool entry ``search_index``
    (:func:`jobfucker.config.with_overrides` — the ``search`` preview path
    swaps one entry's query/filter block without touching the stored row), then
    selects the captcha handler and builds the client factory with the same
    fail-fast semantics as the fresh-config path.

    Returns ``Ok((config, factory))``, or ``Err`` when the stored section is
    missing/invalid (re-run ``init``), an override fails validation,
    or no captcha handler can be selected.
    """
    refs = replace(
        to_persisted_refs(snapshot),
        name=pipeline.name,
        description=pipeline.description,
    )
    config_result = build_config_from_refs(refs)
    if config_result.is_err:
        return Err(config_result.unwrap_err())
    config = config_result.unwrap()

    if query_override is not None or filter_override is not None:
        overridden = with_overrides(
            config, search_index=search_index, query=query_override, filter_data=filter_override
        )
        if overridden.is_err:
            return Err(overridden.unwrap_err())
        config = overridden.unwrap()

    # The factory needs the same captcha fail-fast + --no-captcha-ai semantics
    # as the fresh-config bootstrapping path, but driven by the reconstructed
    # config (whose openai_captcha comes from the DB, not a fresh pipeline.yaml).
    selected = select_captcha_handler(
        config,
        use_sixel=use_sixel,
        use_kitty=use_kitty,
        no_captcha_ai=no_captcha_ai,
    )
    if selected.is_err:
        if require_captcha:
            return Err(selected.unwrap_err())
        # AI-only stages (score/generate) never construct or use a client, so a
        # missing captcha handler must not block them: fall back to the no-op
        # handler used by config-less commands. It is never invoked.
        handler = _fallback_captcha_handler()
    else:
        handler = selected.unwrap()

    factory = _build_factory(
        TerminalAuthInteraction(),
        handler,
        config.service,
        ClientCredentials(login=config.auth.login, password=config.auth.password),
        section=config.service_section,
        reporter=reporter,
    )
    return Ok((config, factory))


def build_config_from_pipeline(
    pipeline: Pipeline,
    snapshot: PipelineSnapshot,
) -> Result[PipelineConfig, str]:
    """Reconstruct the validated :class:`PipelineConfig` for a stored pipeline.

    Zero file I/O ("we trust the db"); no factory, no captcha selection. Used
    by commands that need to *read* the reconstructed config (e.g. ``search``
    resolves the search pool before building a client).
    """
    refs = replace(
        to_persisted_refs(snapshot),
        name=pipeline.name,
        description=pipeline.description,
    )
    return build_config_from_refs(refs)


def build_client_from_pipeline(
    pipeline: Pipeline,
    snapshot: PipelineSnapshot,
    *,
    use_sixel: bool = False,
    use_kitty: bool = False,
    no_captcha_ai: bool = False,
    search_index: int = 0,
    query_override: str | None = None,
    filter_override: Mapping[str, object] | None = None,  # lint-ignore[restricted-object]: YAML payload boundary
    reporter: Reporter | None = None,
) -> Result[Client, str]:
    """Build a single client for a stored pipeline (config-from-DB path).

    Used by commands that talk to the board directly without running a full
    stage pipeline (e.g. ``jobfucker resumes``, ``jobfucker search``).
    ``query_override`` replaces pool entry ``search_index``'s query and
    ``filter_override`` swaps its filter block on the reconstructed config
    before the client is constructed (see
    :func:`jobfucker.config.with_overrides`); an invalid override fails the
    build before any client or network work happens. The caller owns the
    client lifecycle and must ``aclose()`` it.
    """
    config_factory = _rebuild_config_and_factory(
        pipeline,
        snapshot,
        use_sixel=use_sixel,
        use_kitty=use_kitty,
        no_captcha_ai=no_captcha_ai,
        search_index=search_index,
        query_override=query_override,
        filter_override=filter_override,
        reporter=reporter,
    )
    if config_factory.is_err:
        return Err(config_factory.unwrap_err())
    config, factory = config_factory.unwrap()
    return Ok(factory.get(config.service))


def build_engine_from_pipeline(
    storage: Storage,
    pipeline: Pipeline,
    snapshot: PipelineSnapshot,
    *,
    use_sixel: bool = False,
    use_kitty: bool = False,
    no_captcha_ai: bool = False,
    require_captcha: bool = True,
    ai_override: AiClient | None = None,
    progress: Reporter | None = None,
) -> Result[Engine, str]:
    """Rebuild a runnable :class:`Engine` for a stored pipeline + its snapshot.

    This is the engine-reconstruction path that lets a stage run by
    ``--pipeline-id`` with **no fresh config object** (config-from-DB): the
    snapshot's CONTENT columns (login/password/resume/api-key/prompt text) are
    marshalled into a :class:`PersistedPipelineRefs` view, the
    ``service_section`` and ``openai_captcha`` JSON columns are re-validated
    through the registry, and a :class:`PipelineConfig` is assembled — with
    **zero file I/O** ("we trust the db"), so a run works even if the original
    referenced files were moved or deleted — then an :class:`Engine` is built
    from it exactly as the fresh-config path does.

    Because ``to_persisted_refs`` leaves the identity ``name``/``description``
    empty (they live on the :class:`Pipeline` identity, not the snapshot), those
    two labels are merged in from ``pipeline`` before the config is built. The
    snapshot's ``id`` is threaded into the :class:`Engine` so every row the
    stage run writes is stamped with the provenance ``*_snapshot_id``.

    Captcha selection then runs on the reconstructed config with the same
    fail-fast semantics and ``--no-captcha-ai`` behaviour as the old
    :func:`bootstrap_cli` (removed in Slice B). The factory is built with the
    available clients registered, and
    the AI client is ``ai_override`` when given, else an :class:`OpenAIWrapper`
    over the reconstructed ``openai`` config.

    Args:
        storage: the storage facade (same one the command already uses).
        pipeline: the identity DTO (carries ``name``/``description`` for the
            rebuilt config label; its ``id`` stays the run's identity).
        snapshot: the resolved :class:`PipelineSnapshot` whose config content
            drives the run (its ``id`` becomes the engine's ``snapshot_id``).
        use_sixel: force the terminal sixel captcha handler.
        use_kitty: force the terminal kitty captcha handler.
        no_captcha_ai: disable the AI captcha branch even when the stored
            ``openai_captcha`` section is configured.
        require_captcha: ``True`` (default) keeps the documented fail-fast when
            no captcha handler resolves (client-constructing stages). ``False``
            lets an AI-only stage (``score``/``generate``) build even when no
            handler can be selected — those stages never construct/use a client,
            so it falls back to the no-op fallback handler (never invoked)
            instead of returning ``Err``.
        ai_override: an injected :class:`AiClient` (tests stub scoring to avoid
            network); defaults to a real :class:`OpenAIWrapper` over the
            reconstructed ``openai`` config.
        progress: the live output sink forwarded into the rebuilt :class:`Engine`
            (see :class:`~jobfucker.engine.Engine`); omitted means a no-op sink,
            so passthrough/aggregate consumers keep working unchanged.

    Returns:
        ``Ok(Engine)``, or ``Err`` when the stored section is missing or fails
        re-validation (re-run ``init`` to fix), or no captcha handler can be
        selected (the documented fail-fast case).
    """
    config_factory = _rebuild_config_and_factory(
        pipeline,
        snapshot,
        use_sixel=use_sixel,
        use_kitty=use_kitty,
        no_captcha_ai=no_captcha_ai,
        require_captcha=require_captcha,
        reporter=progress,
    )
    if config_factory.is_err:
        return Err(config_factory.unwrap_err())
    config, factory = config_factory.unwrap()
    ai = ai_override if ai_override is not None else OpenAIWrapper(config.openai)
    return Ok(
        Engine(
            storage=storage,
            factory=factory,
            config=config,
            ai=ai,
            snapshot_id=snapshot.id,
            progress=progress,
        )
    )
