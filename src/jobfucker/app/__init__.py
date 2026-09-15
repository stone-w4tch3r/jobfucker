"""Application/service layer (Slice A bootstrap).

The explicit ``app/`` layer owns pipeline lifecycle + config↔storage mapping and
provides a single **UI-neutral** composition root (:class:`AppServices`) that
both the CLI and the Qt GUI consume after Slice B rewires them.

Modules:
- :mod:`jobfucker.app.mapping` — config↔storage DTO mapping (both directions);
- :mod:`jobfucker.app.pipeline_service` — create / get-or-keep / append snapshots;
- :mod:`jobfucker.app.services` — :class:`AppServices` + :class:`NoopAuthInteraction`.
"""

from __future__ import annotations

from jobfucker.app.mapping import build_config_from_refs, new_identity, to_persisted_refs, to_pipeline_snapshot
from jobfucker.app.pipeline_service import PipelineMutationResult, PipelineService
from jobfucker.app.services import AppServices, NoopAuthInteraction

__all__ = [
    "AppServices",
    "NoopAuthInteraction",
    "PipelineMutationResult",
    "PipelineService",
    "build_config_from_refs",
    "new_identity",
    "to_persisted_refs",
    "to_pipeline_snapshot",
]
