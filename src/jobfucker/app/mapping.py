"""Config↔storage DTO mapping corridor for the application layer.

The ``app/`` layer is the only place allowed to import BOTH
:mod:`jobfucker.config` (storage-free) and :mod:`jobfucker.storage.dto`. This
module owns both directions of the config↔storage mapping:

- :func:`to_pipeline_snapshot` — forward: a validated :class:`PipelineConfig`
  onto a storage :class:`PipelineSnapshot` DTO (config lives in the snapshot
  now, not the identity row);
- :func:`to_persisted_refs` — a stored :class:`PipelineSnapshot` onto the
  :class:`PersistedPipelineRefs` view (consumed by :func:`config.reconstruct_pipeline_config`);
- :func:`build_config_from_refs` — reverse: reconstruct a
  :class:`PipelineConfig` from the decoupled refs view;
- :func:`new_identity` — a fresh :class:`Pipeline` **identity** DTO (idless,
  no config) for creating a stable pipeline label.

``config.py`` stays storage-free; this module is the corridor that bridges the
two. No import cycles: ``app/`` imports config + storage.dto, never the other
way around.
"""

from __future__ import annotations

from rusty_results.prelude import Result

from jobfucker.config import (
    PersistedPipelineRefs,
    PipelineConfig,
    dump_hh_test_solving,
    dump_openai_captcha,
    dump_service_section,
    reconstruct_pipeline_config,
)
from jobfucker.storage.dto import Pipeline, PipelineSnapshot

__all__ = [
    "build_config_from_refs",
    "new_identity",
    "to_persisted_refs",
    "to_pipeline_snapshot",
]


def to_pipeline_snapshot(config: PipelineConfig) -> PipelineSnapshot:
    """Map a validated config onto a storage :class:`PipelineSnapshot` DTO.

    The snapshot row columns are the source of truth and hold the loaded
    file CONTENTS directly (loaded once at init/update): ``login``/``password``
    = auth contents, ``resume`` = resume markdown contents,
    ``openai_api_key`` = openai api_key content, ``scoring_prompt`` /
    ``apply_prompt`` = prompt template contents. The validated
    ``service.<board>`` section is stored as opaque JSON (``service_section``)
    and the optional AI-captcha block as ``openai_captcha`` JSON. A later
    ``--pipeline-id`` stage run reconstructs the config from the head snapshot
    with **zero file I/O** ("we trust the db").

    Identity/snapshot bookkeeping fields are inert placeholders here —
    ``pipeline_id=0``/``snapshot_no=0``/``source='manual'``/``note=None``/
    ``created_at=''`` — to be filled in by the caller (pipeline service/repo)
    before persist.
    """
    return PipelineSnapshot(
        id=0,
        pipeline_id=0,
        snapshot_no=0,
        source="manual",
        note=None,
        service=config.service,
        service_section=dump_service_section(config.service_section),
        openai_captcha=dump_openai_captcha(config.openai_captcha),
        hh_test_solving=dump_hh_test_solving(config.hh_test_solving),
        login=config.auth.login,
        password=config.auth.password,
        resume=config.resume.contents,
        openai_model=config.openai.model,
        openai_base_url=config.openai.base_url,
        openai_api_key=config.openai.api_key or "",
        openai_reasoning_effort=config.openai.reasoning_effort,
        min_required_score=config.scoring.min_required_score,
        scoring_prompt=config.scoring.scoring_prompt,
        apply_prompt=config.apply.apply_prompt,
        daily_apply_limit=config.limits.daily_apply_limit,
        created_at="",
    )


def new_identity(name: str, description: str | None) -> Pipeline:
    """Build a fresh, idless :class:`Pipeline` **identity** DTO for persisting.

    The identity carries only the user label (``name``/``description``); its
    config lives in snapshots. Timestamps are inert (DB server defaults); the id
    is assigned on insert, and ``current_snapshot_id`` is set once the first
    snapshot exists.
    """
    return Pipeline(
        id=0,
        name=name,
        description=description,
        current_snapshot_id=None,
        created_at="",
        updated_at="",
        soft_deleted_at=None,
    )


def to_persisted_refs(snapshot: PipelineSnapshot) -> PersistedPipelineRefs:
    """Map a stored :class:`PipelineSnapshot` onto a :class:`PersistedPipelineRefs` view.

    The snapshot's scalar + CONTENT columns (login/password/resume/api-key/prompt
    text) plus the opaque ``service_section`` / ``openai_captcha`` JSON columns
    are marshalled into the decoupled view that :func:`config.reconstruct_pipeline_config`
    consumes to rebuild a config with zero file I/O.
    """
    return PersistedPipelineRefs(
        name="",
        description=None,
        service=snapshot.service,
        login=snapshot.login,
        password=snapshot.password,
        resume=snapshot.resume,
        openai_model=snapshot.openai_model,
        openai_base_url=snapshot.openai_base_url,
        openai_api_key=snapshot.openai_api_key,
        openai_reasoning_effort=snapshot.openai_reasoning_effort,
        min_required_score=snapshot.min_required_score,
        scoring_prompt=snapshot.scoring_prompt,
        apply_prompt=snapshot.apply_prompt,
        daily_apply_limit=snapshot.daily_apply_limit,
        service_section=snapshot.service_section,
        openai_captcha=snapshot.openai_captcha,
        hh_test_solving=snapshot.hh_test_solving,
    )


def build_config_from_refs(refs: PersistedPipelineRefs) -> Result[PipelineConfig, str]:
    """Reconstruct a validated :class:`PipelineConfig` from a stored row's refs.

    Thin alias for :func:`config.reconstruct_pipeline_config`: the reverse of
    :func:`to_pipeline_snapshot`/:func:`to_persisted_refs`. Performs zero file I/O.
    """
    return reconstruct_pipeline_config(refs)
