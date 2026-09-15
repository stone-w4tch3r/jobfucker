"""Config-from-DB: rebuild an engine from a stored pipeline + snapshot (Task 6).

This is the canonical config-from-DB suite (test plan §4). It proves a stage
can run by ``--pipeline-id`` with **no fresh config object**:

1. ``init`` stores the validated config with the loaded file CONTENTS in the
   snapshot row columns (``login``/``password``/``resume``/``openai_api_key``/
   ``scoring_prompt``/``apply_prompt``), the opaque ``service_section``
   JSON, and the optional ``openai_captcha`` JSON — the snapshot is the source
   of truth, with **no JSON snapshot duplication**;
2. ``bootstrap.build_engine_from_pipeline`` rebuilds an :class:`Engine` from the
   identity :class:`Pipeline` + its resolved :class:`PipelineSnapshot` by
   marshalling the snapshot into :class:`PersistedPipelineRefs` (merging the
   identity's ``name``/``description``) and re-validating the stored section +
   captcha with **zero file I/O** ("we trust the db"), so a run works even if
   the original referenced files were moved or deleted. The snapshot's ``id``
   is threaded into the engine so every row the run writes is stamped.

Uses real temporary files (real-files philosophy), the real repository, the real
factory (mock registered) and a stub :class:`AiClient` (``ai_override``) — the
same shape the engine tests already use, minus the fresh ``PipelineConfig`` at
the call site. Captcha outcomes are forced deterministically via
``use_sixel=True`` or by patching ``detect_terminal_protocol``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from rusty_results.prelude import Ok

from jobfucker.ai import ScoreResult
from jobfucker.bootstrap import build_engine_from_pipeline
from jobfucker.clients.base import ClientDeps
from jobfucker.clients.mock.params import MockApplyBehaviorConfig, MockBehaviorConfig
from jobfucker.config import load_service_section
from jobfucker.stages.apply import ApplyReport
from jobfucker.stages.score import ScoreReport
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, PipelineSnapshot
from test.pipeline_helpers import ScriptedAi, build_pipeline_config, create_pipeline, snapshot_for, store_pipeline
from test.storage.builders import make_pipeline


def _write_referenced_files(tmp_path: Path) -> None:
    """Write the referenced files a real ``pipeline.yaml`` would point at."""
    (tmp_path / "resume.md").write_text("resume contents", encoding="utf-8")
    (tmp_path / "score.j2").write_text("Score {{ vacancy_formatted }}", encoding="utf-8")
    (tmp_path / "apply.j2").write_text("Apply for {{ vacancy_formatted }}", encoding="utf-8")
    (tmp_path / "key.txt").write_text("test-secret", encoding="utf-8")
    (tmp_path / "login.txt").write_text("u", encoding="utf-8")
    (tmp_path / "pwd.txt").write_text("p", encoding="utf-8")


def _config_pointing_at(tmp_path: Path, behavior: MockBehaviorConfig | None = None):
    """A validated config whose referenced files live in ``tmp_path``."""
    from jobfucker.config import PipelineConfig

    base = build_pipeline_config(behavior=behavior)
    config: PipelineConfig = base.model_copy(
        update={
            "resume": base.resume.model_copy(update={"path": tmp_path / "resume.md"}),
            "scoring": base.scoring.model_copy(update={"scoring_prompt_file": tmp_path / "score.j2"}),
            "apply": base.apply.model_copy(update={"apply_prompt_file": tmp_path / "apply.j2"}),
            "openai": base.openai.model_copy(update={"api_key_file": tmp_path / "key.txt"}),
            "auth": base.auth.model_copy(
                update={"login_file": tmp_path / "login.txt", "password_file": tmp_path / "pwd.txt"}
            ),
        }
    )
    return config


async def _store_full_pipeline(storage: Storage, tmp_path: Path) -> tuple[Pipeline, PipelineSnapshot]:
    """Mirror what the real ``init`` command does: config -> identity + snapshot.

    Returns the stored identity + head snapshot (the snapshot holds the CONTENTS
    in its columns + the opaque section + optional captcha JSON).
    """
    _write_referenced_files(tmp_path)
    return await store_pipeline(storage, _config_pointing_at(tmp_path))


async def _store_full_pipeline_with_behavior(
    storage: Storage, tmp_path: Path, behavior: MockBehaviorConfig
) -> tuple[Pipeline, PipelineSnapshot]:
    """Same as :func:`_store_full_pipeline` but carries a mock ``behavior`` into
    the stored ``service_section`` (proving the behavior seam survives the
    config -> identity/snapshot -> DB -> rebuild round trip).
    """
    _write_referenced_files(tmp_path)
    return await store_pipeline(storage, _config_pointing_at(tmp_path, behavior))


# --- 4.1 graduated prototype: full stage run by --pipeline-id, no fresh config
@pytest.mark.integration
async def test_init_equivalent_then_stage_runs_by_pipeline_id_no_config(storage: Storage, tmp_path: Path) -> None:
    stored, snapshot = await _store_full_pipeline(storage, tmp_path)

    # The DB now holds the opaque section JSON + the CONTENTS in the snapshot
    # columns (resume/api-key/apply prompt); re-validate the section from it.
    assert snapshot.service_section is not None
    assert snapshot.resume == "resume contents"
    assert snapshot.openai_api_key == "test-key"
    assert snapshot.apply_prompt == "Apply for {{ vacancy_formatted }}"
    section_result = load_service_section(snapshot.service, snapshot.service_section)
    assert section_result.is_ok
    section = section_result.unwrap()
    assert section is not None
    assert section.resume_id == "mock-resume-1"

    # Re-read the identity + head snapshot fresh from the DB by id (simulates a
    # later invocation).
    reloaded = await storage.pipelines.get(stored.id)
    assert reloaded is not None
    reloaded_snapshot = await storage.snapshots.current(reloaded.id)
    assert reloaded_snapshot is not None
    assert reloaded_snapshot.id == snapshot.id

    # A stage runs by --pipeline-id alone: no fresh PipelineConfig. The engine
    # is rebuilt from the identity + snapshot with the stub AI via ai_override.
    result = build_engine_from_pipeline(storage, reloaded, reloaded_snapshot, use_sixel=True, ai_override=ScriptedAi())
    assert result.is_ok, result.unwrap_err()
    engine = result.unwrap()

    fetch = await engine.fetch(reloaded)
    assert fetch.is_ok
    fetch_report = fetch.unwrap()[0]
    # The stored section's ``vacancies`` list carries the 3 canned rows (they
    # live in the snapshot's ``service_section`` JSON, not in a fixture file).
    assert fetch_report.fetched == 3

    score = await engine.score(reloaded)
    assert score.is_ok
    score_report: ScoreReport = score.unwrap()
    assert score_report.scored == 3

    cv = await engine.generate_cv(reloaded)
    assert cv.is_ok
    assert cv.unwrap().generated == 3

    apply = await engine.apply(reloaded)
    assert apply.is_ok
    assert apply.unwrap().applied == 3

    # Every row is stamped with the resolved snapshot (spec success criterion 3).
    for vacancy in await storage.vacancies.list_by_pipeline(reloaded.id):
        assert vacancy.fetched_snapshot_id == snapshot.id
        assert vacancy.scored_snapshot_id == snapshot.id
        assert vacancy.generated_snapshot_id == snapshot.id
        assert vacancy.applied_snapshot_id == snapshot.id


# --- 4.2 contents-in-DB: a --pipeline-id run works even if the files are gone
@pytest.mark.integration
async def test_rebuild_works_when_referenced_files_deleted(storage: Storage, tmp_path: Path) -> None:
    stored, snapshot = await _store_full_pipeline(storage, tmp_path)

    # Delete every referenced file after init; rebuild must still succeed because
    # the loaded CONTENTS live in the snapshot columns, not on disk (no file I/O
    # at rebuild — no JSON snapshot to keep in sync).
    for name in ("resume.md", "score.j2", "apply.j2", "key.txt", "login.txt", "pwd.txt"):
        (tmp_path / name).unlink()

    result = build_engine_from_pipeline(storage, stored, snapshot, use_sixel=True, ai_override=ScriptedAi())
    assert result.is_ok, result.unwrap_err()
    assert (await result.unwrap().fetch(stored)).unwrap()[0].fetched == 3


async def _stored_row_with_section(storage: Storage, section: str | None) -> tuple[Pipeline, PipelineSnapshot]:
    """A valid identity + snapshot (content columns populated from the DTO
    defaults) with ``service_section`` overridden.

    ``reconstruct_pipeline_config`` re-validates the stored section first, so
    reaching the section error path only needs a non-empty valid content row.
    """
    identity = await storage.pipelines.create(make_pipeline(name="overridden-section"))
    base = build_pipeline_config(name="overridden-section")
    from dataclasses import replace

    from test.pipeline_helpers import build_pipeline_snapshot

    # Snapshot #1 (valid section) then #2 with the overridden section, so the
    # UNIQUE(pipeline_id, snapshot_no) constraint is never hit.
    await storage.snapshots.create(build_pipeline_snapshot(base, pipeline_id=identity.id, snapshot_no=1))
    base_2 = build_pipeline_snapshot(base, pipeline_id=identity.id, snapshot_no=2)
    overridden = await storage.snapshots.create(replace(base_2, service_section=section))
    await storage.pipelines.set_current_snapshot(identity.id, overridden.id)
    return await storage.pipelines.get(identity.id) or identity, await storage.snapshots.current(
        identity.id
    ) or overridden


# --- 4.3 bad / missing stored service_section -> clear Err
@pytest.mark.integration
async def test_rebuild_bad_stored_section_returns_clear_err(storage: Storage) -> None:
    bad_identity, bad_snapshot = await _stored_row_with_section(storage, "{not valid json")
    result = build_engine_from_pipeline(storage, bad_identity, bad_snapshot, use_sixel=True, ai_override=ScriptedAi())
    assert result.is_err
    assert "is not valid JSON" in result.unwrap_err()


@pytest.mark.integration
async def test_rebuild_null_stored_section_returns_clear_err(storage: Storage) -> None:
    no_identity, no_snapshot = await _stored_row_with_section(storage, None)
    result = build_engine_from_pipeline(storage, no_identity, no_snapshot, use_sixel=True, ai_override=ScriptedAi())
    assert result.is_err
    assert "has no stored service section; re-run init" in result.unwrap_err()


# --- captcha behaviour from the persisted openai_captcha + flags
@pytest.mark.integration
async def test_no_captcha_configured_terminal_fallback(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # openai_captcha is None (build_pipeline_config default). Without AI captcha
    # the terminal/detect path decides; force a detectable protocol -> Ok.
    stored, snapshot = await _store_full_pipeline(storage, tmp_path)
    from jobfucker.captcha import selector

    def _detect_sixel(*, env: Mapping[str, str] | None = None, capabilities_path: Path | None = None) -> str:
        del env, capabilities_path
        return "sixel"

    monkeypatch.setattr(selector, "detect_terminal_protocol", _detect_sixel)
    result = build_engine_from_pipeline(storage, stored, snapshot, ai_override=ScriptedAi())
    assert result.is_ok, result.unwrap_err()
    assert (await result.unwrap().fetch(stored)).unwrap()[0].fetched == 3


@pytest.mark.integration
async def test_no_captcha_configured_fail_fast(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # openai_captcha is None and no terminal protocol detectable -> Err
    # (the documented fail-fast), not a silently-broken handler.
    stored, snapshot = await _store_full_pipeline(storage, tmp_path)
    from jobfucker.captcha import selector

    def _none_detected(*, env: Mapping[str, str] | None = None, capabilities_path: Path | None = None) -> None:
        del env, capabilities_path

    monkeypatch.setattr(selector, "detect_terminal_protocol", _none_detected)
    result = build_engine_from_pipeline(storage, stored, snapshot, ai_override=ScriptedAi())
    assert result.is_err
    assert "протокол" in result.unwrap_err()


@pytest.mark.integration
async def test_rebuild_require_captcha_false_succeeds_when_none_detectable(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The anomaly this fixes: in a non-terminal env with no openai_captcha, the
    # default (require_captcha=True) fails fast. AI-only stages (score/generate)
    # never touch a client, so they build with require_captcha=False and fall
    # back to the no-op handler instead of Err.
    stored, snapshot = await _store_full_pipeline(storage, tmp_path)
    from jobfucker.captcha import selector

    def _none_detected(*, env: Mapping[str, str] | None = None, capabilities_path: Path | None = None) -> None:
        del env, capabilities_path

    monkeypatch.setattr(selector, "detect_terminal_protocol", _none_detected)

    # Default stays fail-fast for client-constructing stages.
    err = build_engine_from_pipeline(storage, stored, snapshot, ai_override=ScriptedAi())
    assert err.is_err
    assert "протокол" in err.unwrap_err()

    # require_captcha=False still builds a working engine (handler never used).
    ok = build_engine_from_pipeline(storage, stored, snapshot, require_captcha=False, ai_override=ScriptedAi())
    assert ok.is_ok, ok.unwrap_err()
    assert (await ok.unwrap().fetch(stored)).unwrap()[0].fetched == 3


@pytest.mark.integration
async def test_no_captcha_ai_forces_terminal_even_when_ai_configured(
    storage: Storage, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Configure openai_captcha (AI branch available) but pass --no-captcha-ai:
    # the AI branch is skipped and the terminal/auto-detect path decides.
    _write_referenced_files(tmp_path)
    from jobfucker.config import OpenAIConfig

    base = _config_pointing_at(tmp_path)
    captcha = OpenAIConfig(
        model="gpt-captcha",
        base_url="https://api.example.com/v1",
        api_key_file=tmp_path / "key.txt",
    )
    config = base.model_copy(update={"openai_captcha": captcha})
    stored, snapshot = await store_pipeline(storage, config)

    from jobfucker.captcha import selector

    def _detect_sixel(*, env: Mapping[str, str] | None = None, capabilities_path: Path | None = None) -> str:
        del env, capabilities_path
        return "sixel"

    monkeypatch.setattr(selector, "detect_terminal_protocol", _detect_sixel)
    result = build_engine_from_pipeline(storage, stored, snapshot, no_captcha_ai=True, ai_override=ScriptedAi())
    assert result.is_ok, result.unwrap_err()
    assert (await result.unwrap().fetch(stored)).unwrap()[0].fetched == 3


@pytest.mark.integration
async def test_ai_branch_uses_injected_ai_override(storage: Storage, tmp_path: Path) -> None:
    # The engine must use the injected ai_override (never a real OpenAIWrapper),
    # proven by the scripted reasoning text being persisted by the score stage.
    stored, snapshot = await _store_full_pipeline(storage, tmp_path)
    scripted = ScriptedAi(
        scores=[
            Ok(ScoreResult(fit_score=5, comment="from-injected-ai")),
            Ok(ScoreResult(fit_score=5, comment="from-injected-ai")),
            Ok(ScoreResult(fit_score=5, comment="from-injected-ai")),
        ]
    )
    result = build_engine_from_pipeline(storage, stored, snapshot, use_sixel=True, ai_override=scripted)
    assert result.is_ok, result.unwrap_err()
    engine = result.unwrap()
    assert (await engine.fetch(stored)).unwrap()[0].fetched == 3
    assert (await engine.score(stored)).unwrap().scored == 3
    rows = await storage.vacancies.list_by_pipeline(stored.id)
    assert len(rows) == 3
    assert all(row.score_reasoning == "from-injected-ai" for row in rows)


@pytest.mark.integration
async def test_use_sixel_and_use_kitty_are_mutually_exclusive(storage: Storage, tmp_path: Path) -> None:
    stored, snapshot = await _store_full_pipeline(storage, tmp_path)
    result = build_engine_from_pipeline(
        storage, stored, snapshot, use_sixel=True, use_kitty=True, ai_override=ScriptedAi()
    )
    assert result.is_err
    assert "Cannot use both --use-sixel and --use-kitty" in result.unwrap_err()


# --- behavior flows config -> DB (stored section) -> client -> engine, no core knowledge
@pytest.mark.integration
async def test_apply_reflects_default_apply_error_from_stored_section(storage: Storage, tmp_path: Path) -> None:
    # The stored section carries a default apply behavior of "error", so every
    # eligible vacancy fails to apply. The behavior must survive the config ->
    # identity/snapshot -> DB -> rebuild round trip with zero core awareness of
    # the mock seam.
    stored, snapshot = await _store_full_pipeline_with_behavior(
        storage, tmp_path, MockBehaviorConfig(default_apply=MockApplyBehaviorConfig(outcome="error"))
    )

    result = build_engine_from_pipeline(storage, stored, snapshot, use_sixel=True, ai_override=ScriptedAi())
    assert result.is_ok, result.unwrap_err()
    engine = result.unwrap()

    assert (await engine.fetch(stored)).unwrap()[0].fetched == 3
    assert (await engine.score(stored)).unwrap().scored == 3
    assert (await engine.generate_cv(stored)).unwrap().generated == 3

    apply = await engine.apply(stored)
    assert apply.is_ok
    apply_report: ApplyReport = apply.unwrap()
    assert apply_report.failed == 3
    assert apply_report.limit_reached is False


@pytest.mark.integration
async def test_apply_limit_exceeded_from_stored_section_stops(storage: Storage, tmp_path: Path) -> None:
    # The first vacancy hits "limit_exceeded"; apply stops early at the first
    # position, so nothing is applied and all 3 candidates stay pending.
    stored, snapshot = await _store_full_pipeline_with_behavior(
        storage,
        tmp_path,
        MockBehaviorConfig(per_vacancy={"mock-1": MockApplyBehaviorConfig(outcome="limit_exceeded")}),
    )

    result = build_engine_from_pipeline(storage, stored, snapshot, use_sixel=True, ai_override=ScriptedAi())
    assert result.is_ok, result.unwrap_err()
    engine = result.unwrap()

    assert (await engine.fetch(stored)).unwrap()[0].fetched == 3
    assert (await engine.score(stored)).unwrap().scored == 3
    assert (await engine.generate_cv(stored)).unwrap().generated == 3

    apply = await engine.apply(stored)
    assert apply.is_ok
    apply_report: ApplyReport = apply.unwrap()
    assert apply_report.limit_reached is True
    assert apply_report.applied == 0
    assert apply_report.pending == 3


# --- search-preview overrides reach the built client --------------------------
@pytest.mark.integration
async def test_client_build_applies_query_and_filter_overrides(storage: Storage, client_deps: ClientDeps) -> None:
    """``--query``/``--params`` land in the client's prebuilt entry, not just the output."""
    from jobfucker.bootstrap import build_client_from_pipeline

    pipeline = await create_pipeline(storage)
    snapshot = await snapshot_for(storage, pipeline)

    result = build_client_from_pipeline(
        pipeline,
        snapshot,
        search_index=0,
        query_override="overridden-query",
        filter_override={"area": [40]},
        use_sixel=True,  # captcha handler seam; never solved by this test
    )
    assert result.is_ok, result.unwrap_err()
    client = result.unwrap()
    try:
        from jobfucker.clients.mock.client import MockClient

        assert isinstance(client, MockClient)
        entry = client._entries[0]  # pyright: ignore[reportPrivateUsage]  # rationale: regression probe of the prebuilt pair
        assert entry.query == "overridden-query"
        assert entry.filter.area == (40,)
    finally:
        await client.aclose()
