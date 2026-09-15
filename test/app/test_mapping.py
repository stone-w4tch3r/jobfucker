"""Unit tests for the config↔storage mapping corridor (``app/mapping.py``).

Pure data-transformation tests: no DB, no fixtures from ``test/conftest.py``.
The transforms are lossless in one direction (config → snapshot → refs:
contents + scalars + section JSON preserved) while file paths are deliberately
not restored (the row stores CONTENTS, never paths).

Post-snapshot rewrite, :func:`to_pipeline_snapshot` maps a :class:`PipelineConfig`
onto a :class:`PipelineSnapshot` (config lives in the snapshot, not the identity
row), and :func:`to_persisted_refs` consumes a snapshot. Identity fields (name/
description) live on the pipeline, so they are not round-tripped through the
snapshot path and are deliberately not asserted here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobfucker.app.mapping import (
    build_config_from_refs,
    new_identity,
    to_persisted_refs,
    to_pipeline_snapshot,
)
from jobfucker.clients.mock.params import MockServiceConfig
from jobfucker.config import (
    OpenAIConfig,
    PipelineConfig,
    dump_openai_captcha,
    dump_service_section,
)
from jobfucker.storage.dto import PipelineSnapshot
from test.pipeline_helpers import build_pipeline_config
from test.storage.builders import make_snapshot


@pytest.fixture
def config() -> PipelineConfig:
    """A validated mock pipeline config with the default section."""
    return build_pipeline_config()


@pytest.fixture
def config_captcha(config: PipelineConfig) -> PipelineConfig:
    """``config`` with a configured ``openai_captcha`` section."""
    return config.model_copy(
        update={
            "openai_captcha": OpenAIConfig(
                model="gpt-captcha",
                base_url="https://api.openai.com/v1",
                api_key="cap-key",
            )
        }
    )


@pytest.fixture
def config_no_key(config: PipelineConfig) -> PipelineConfig:
    """``config`` with the main ``openai.api_key`` explicitly ``None``."""
    return config.model_copy(update={"openai": config.openai.model_copy(update={"api_key": None})})


def _assert_content_scalar_parity(snapshot: PipelineSnapshot, config: PipelineConfig) -> None:
    """Assert every content + scalar field of ``snapshot`` equals its config source."""
    assert snapshot.service == config.service
    assert snapshot.login == config.auth.login
    assert snapshot.password == config.auth.password
    assert snapshot.resume == config.resume.contents
    assert snapshot.openai_model == config.openai.model
    assert snapshot.openai_base_url == config.openai.base_url
    assert snapshot.openai_api_key == (config.openai.api_key or "")
    assert snapshot.openai_reasoning_effort == config.openai.reasoning_effort
    assert snapshot.min_required_score == config.scoring.min_required_score
    assert snapshot.scoring_prompt == config.scoring.scoring_prompt
    assert snapshot.apply_prompt == config.apply.apply_prompt
    assert snapshot.daily_apply_limit == config.limits.daily_apply_limit


@pytest.mark.unit
def test_to_pipeline_snapshot_maps_all_content_and_scalar_fields(config: PipelineConfig) -> None:
    snap = to_pipeline_snapshot(config)
    assert isinstance(snap, PipelineSnapshot)
    _assert_content_scalar_parity(snap, config)
    assert snap.service_section == dump_service_section(config.service_section)
    assert isinstance(snap.service_section, str)
    assert json.loads(snap.service_section)["resume_id"] == "mock-resume-1"
    assert snap.openai_captcha is None
    # inert snapshot bookkeeping placeholders (filled in by the service before persist)
    assert snap.id == 0
    assert snap.pipeline_id == 0
    assert snap.snapshot_no == 0
    assert snap.source == "manual"
    assert snap.note is None
    assert snap.created_at == ""


@pytest.mark.unit
def test_to_pipeline_snapshot_serializes_openai_captcha_section(config_captcha: PipelineConfig) -> None:
    snap = to_pipeline_snapshot(config_captcha)
    assert snap.openai_captcha == dump_openai_captcha(config_captcha.openai_captcha)
    assert isinstance(snap.openai_captcha, str)
    assert json.loads(snap.openai_captcha)["api_key"] == "cap-key"


@pytest.mark.unit
def test_to_pipeline_snapshot_none_api_key_becomes_empty_string(config_no_key: PipelineConfig) -> None:
    snap = to_pipeline_snapshot(config_no_key)
    assert snap.openai_api_key == ""


@pytest.mark.unit
def test_to_pipeline_snapshot_persists_openai_reasoning_effort(config: PipelineConfig) -> None:
    """A configured ``reasoning_effort`` lands on the snapshot scalar column."""
    cfg = config.model_copy(update={"openai": config.openai.model_copy(update={"reasoning_effort": "medium"})})
    snap = to_pipeline_snapshot(cfg)
    assert snap.openai_reasoning_effort == "medium"
    _assert_content_scalar_parity(snap, cfg)


@pytest.mark.unit
def test_round_trip_preserves_content_scalar_and_section_fields(config: PipelineConfig) -> None:
    snap = to_pipeline_snapshot(config)
    rebuilt = build_config_from_refs(to_persisted_refs(snap)).unwrap()
    snap2 = to_pipeline_snapshot(rebuilt)
    # Content + scalar parity with the original config (same as test 1).
    _assert_content_scalar_parity(snap2, config)
    # Section JSON survives the round trip unchanged.
    assert snap2.service_section == snap.service_section
    assert snap2.openai_captcha == snap.openai_captcha
    # Paths are NOT restored: the snapshot stores CONTENTS, never file paths.
    assert rebuilt.auth.login_file == Path()
    assert rebuilt.resume.path == Path()
    assert rebuilt.scoring.scoring_prompt_file is None


@pytest.mark.unit
def test_to_persisted_refs_maps_snapshot_dto_field_for_field() -> None:
    snap = to_pipeline_snapshot(build_pipeline_config())
    refs = to_persisted_refs(snap)
    assert refs.service == snap.service
    assert refs.login == snap.login
    assert refs.password == snap.password
    assert refs.resume == snap.resume
    assert refs.openai_model == snap.openai_model
    assert refs.openai_base_url == snap.openai_base_url
    assert refs.openai_api_key == snap.openai_api_key
    assert refs.openai_reasoning_effort == snap.openai_reasoning_effort
    assert refs.min_required_score == snap.min_required_score
    assert refs.scoring_prompt == snap.scoring_prompt
    assert refs.apply_prompt == snap.apply_prompt
    assert refs.daily_apply_limit == snap.daily_apply_limit
    assert refs.service_section == snap.service_section
    assert refs.openai_captcha == snap.openai_captcha


@pytest.mark.unit
def test_build_config_from_refs_reconstructs_service_section() -> None:
    snap = make_snapshot(
        service_section=json.dumps(
            {
                "resume_id": "r1",
                "searches": [
                    {
                        "query": "python",
                        "filter": {
                            "area": [1],
                            "schedule": ["fullDay"],
                            "experience": "between1And3",
                            "only_with_salary": True,
                        },
                    }
                ],
            }
        )
    )
    c = build_config_from_refs(to_persisted_refs(snap)).unwrap()
    assert c.service == "mock"
    section = c.service_section
    assert isinstance(section, MockServiceConfig)
    assert section.resume_id == "r1"
    assert section.searches[0].filter.experience == "between1And3"


@pytest.mark.unit
def test_build_config_from_refs_errors_on_missing_section() -> None:
    refs = to_persisted_refs(make_snapshot(service_section=None))
    result = build_config_from_refs(refs)
    assert result.is_err
    assert "has no stored service section" in result.unwrap_err()


@pytest.mark.unit
def test_new_identity_maps_label_to_identity_dto(config: PipelineConfig) -> None:
    identity = new_identity(config.name, config.description)
    assert identity.name == config.name
    assert identity.description == config.description
    assert identity.id == 0
    assert identity.current_snapshot_id is None
    assert identity.soft_deleted_at is None
    # identity-only: no config carries over
    assert not hasattr(identity, "query")
