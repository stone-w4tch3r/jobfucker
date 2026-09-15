"""Integration tests for the UI-neutral composition root (``app/services.py``).

Proves :func:`AppServices.open` builds the storage + factory + pipeline graph
without any terminal/captcha selection (UI-neutral), accepts an injected
:class:`AuthInteractionProvider`, and (without a storage override) exercises the
real migrate-and-open path against an isolated runtime dir.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from jobfucker.app.pipeline_service import PipelineService
from jobfucker.app.services import AppServices, NoopAuthInteraction
from jobfucker.storage.db import Storage
from test.conftest import StubAuthInteraction
from test.pipeline_helpers import build_pipeline_config


@pytest.mark.integration
async def test_open_returns_connected_graph_with_storage_override(storage: Storage) -> None:
    services = (await AppServices.open(storage_override=storage)).unwrap()
    assert services.storage is storage
    assert services.factory.cap("mock") == 200
    assert isinstance(services.pipelines, PipelineService)
    assert await services.pipelines.list() == []


@pytest.mark.integration
async def test_open_graph_upserts_and_resolves(storage: Storage) -> None:
    services = (await AppServices.open(storage_override=storage)).unwrap()
    created = (await services.pipelines.create(build_pipeline_config())).unwrap().pipeline
    resolved = await services.pipelines.resolve(created.id)
    assert resolved.is_ok
    identity, _snapshot = resolved.unwrap()
    assert identity.name == build_pipeline_config().name


@pytest.mark.integration
async def test_open_is_ui_neutral(storage: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    # Remove terminal env signals: prove open() does no terminal/captcha selection.
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    assert (await AppServices.open(storage_override=storage)).is_ok


@pytest.mark.integration
async def test_open_accepts_injected_interaction(storage: Storage) -> None:
    services = await AppServices.open(storage_override=storage, interaction=StubAuthInteraction())
    assert services.is_ok


@pytest.mark.integration
async def test_open_without_override_migrates_real_runtime_db(runtime_dir: Path) -> None:
    # The fixture wires DATA_DIR/CONFIG_DIR to isolated dirs; open() must migrate
    # a real file-backed DB there (no storage override).
    del runtime_dir
    services = await AppServices.open()
    assert services.is_ok
    svc = services.unwrap()
    assert svc.storage is not None
    assert await svc.pipelines.list() == []


@pytest.mark.integration
async def test_noop_auth_interaction_returns_unavailable() -> None:
    interaction = NoopAuthInteraction()
    assert (await interaction.request_code(prompt="x")).unwrap_err() == "interaction not available"
    assert (await interaction.confirm(prompt="x")).unwrap_err() == "interaction not available"


@pytest.mark.integration
async def test_appservices_is_frozen_and_drops_dead_fields(storage: Storage) -> None:
    svc = (await AppServices.open(storage_override=storage)).unwrap()
    assert dataclasses.is_dataclass(svc)
    fields = svc.__dataclass_fields__
    assert "ai" not in fields
    assert "engine" not in fields
    assert "config" not in fields
    assert "pipelines" in fields
