"""Pipeline lifecycle service: create / get-or-keep / append snapshots.

The pipeline-snapshots rewrite separates a **stable identity** (``pipelines`` —
``name`` labels it, ``id`` never changes) from its append-only **config
snapshots** (``pipeline_snapshot``). This service owns that lifecycle:

- :meth:`create` — a fresh identity + snapshot #1 (the ``init`` / UI "new" path).
- :meth:`get_or_keep` — the old ``init`` reuse behavior: resolve by ``name``;
  none → :meth:`create`; exists → :meth:`new_snapshot` (which **no-ops** when the
  incoming config is unchanged).
- :meth:`new_snapshot` — the ``update`` path: compare the incoming config against
  the current head snapshot. Differ → append snapshot #(n+1) and repoint
  ``current_snapshot_id`` (never re-ids, never soft-deletes). Identical → drop
  silently (no new row).
- :meth:`resolve` — identity + its current head snapshot (a stage run rebuilds
  the config from that snapshot with zero file I/O).

No-op edits are deduplicated in core (no identical snapshot row ever appended),
and updating never changes ``pipeline.id`` nor soft-deletes the head.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from rusty_results.prelude import Err, Ok, Result

from jobfucker.app.mapping import new_identity, to_pipeline_snapshot
from jobfucker.clients.factory import Factory
from jobfucker.config import PipelineConfig, validate_cap, validate_search_windows
from jobfucker.storage.db import Storage
from jobfucker.storage.dto import Pipeline, PipelineSnapshot

__all__ = ["PipelineMutationResult", "PipelineService"]

# Config-content columns that define "sameness" for no-op dedupe (everything
# except identity/bookkeeping fields id/pipeline_id/snapshot_no/source/note/created_at).
# The search pool (queries + windows + filters) lives inside the opaque
# ``service_section`` JSON, so one field covers it.
_SAME_CONFIG_FIELDS: tuple[str, ...] = (
    "service",
    "service_section",
    "openai_captcha",
    "login",
    "password",
    "resume",
    "openai_model",
    "openai_base_url",
    "openai_api_key",
    "openai_reasoning_effort",
    "min_required_score",
    "scoring_prompt",
    "apply_prompt",
    "hh_test_solving",
    "daily_apply_limit",
)


@dataclass(frozen=True, slots=True)
class PipelineMutationResult:
    """Outcome of a pipeline create/append operation.

    Attributes:
        pipeline: the identity row after the mutation (with its current head).
        snapshot: the head snapshot — the freshly created/appended one when a
            real change happened, else the unchanged current head.
        created: ``True`` when a brand-new identity + snapshot was stored.
        appended: ``True`` when a snapshot was appended to an existing identity.
        unchanged: derived — ``True`` iff neither ``created`` nor ``appended``
            (a no-op edit that was silently dropped in core).
    """

    pipeline: Pipeline
    snapshot: PipelineSnapshot
    created: bool
    appended: bool

    @property
    def unchanged(self) -> bool:
        """``True`` when nothing was written (a deduplicated no-op)."""
        return not self.created and not self.appended


class PipelineService:
    """Owns pipeline identity/snapshot lifecycle against a :class:`Storage`."""

    def __init__(self, storage: Storage, factory: Factory) -> None:
        """Bind the service to the storage + client registry it operates on."""
        self._storage = storage
        self._factory = factory

    async def create(
        self,
        config: PipelineConfig,
        *,
        source: str = "manual",
        note: str | None = None,
        validate: bool = True,
    ) -> Result[PipelineMutationResult, str]:
        """Create a fresh identity + snapshot #1 for ``config``.

        Fails (persisting nothing) when ``validate`` finds a cap violation.
        """
        if validate:
            cap = validate_cap(config, self._factory)
            if cap.is_err:
                return Err(cap.unwrap_err())
            windows = validate_search_windows(config, self._factory)
            if windows.is_err:
                return Err(windows.unwrap_err())

        identity = new_identity(config.name, config.description)
        # ``identity.id`` is 0 here: the repository pins the real id (and
        # snapshot_no=1) onto the snapshot inside one transaction.
        snapshot = self._build_snapshot(config, identity.id, 1, source, note)
        result = await self._storage.snapshots.create_identity_with_snapshot(identity, snapshot)
        if result.is_err:
            return Err(result.unwrap_err())
        updated = result.unwrap()
        head = await self._storage.snapshots.current(updated.id)
        if head is None:
            raise RuntimeError(f"pipeline {updated.id} has no head snapshot after atomic create")
        return Ok(PipelineMutationResult(pipeline=updated, snapshot=head, created=True, appended=False))

    async def get_or_keep(
        self,
        config: PipelineConfig,
        *,
        source: str = "manual",
        note: str | None = None,
        validate: bool = True,
    ) -> Result[PipelineMutationResult, str]:
        """The init path: reuse an existing same-name identity, else create.

        Resolution is by ``name`` (the user label). When an identity exists and
        the incoming config is unchanged it is kept as-is (a no-op); when it
        differs a snapshot is appended. Grammar follows :meth:`create`'s error
        behaviour on a cap violation.
        """
        existing = await self.find_by_name(config.name)
        if existing is None:
            return await self.create(config, source=source, note=note, validate=validate)
        return await self.new_snapshot(existing.id, config, source=source, note=note, validate=validate)

    async def new_snapshot(
        self,
        pipeline_id: int,
        config: PipelineConfig,
        *,
        source: str = "manual",
        note: str | None = None,
        validate: bool = True,
    ) -> Result[PipelineMutationResult, str]:
        """Append a snapshot for ``config`` under ``pipeline_id`` (or no-op).

        Compares the incoming config to the current head snapshot. Identical →
        return unchanged WITHOUT writing. Differ → append
        ``snapshot_no = next_snapshot_no`` and repoint ``current_snapshot_id``
        (both in one atomic repository transaction). Never re-ids and never
        soft-deletes. Fails on a cap violation (validate), an unknown or
        soft-deleted pipeline, persisting nothing.
        """
        if validate:
            cap = validate_cap(config, self._factory)
            if cap.is_err:
                return Err(cap.unwrap_err())
            windows = validate_search_windows(config, self._factory)
            if windows.is_err:
                return Err(windows.unwrap_err())

        identity = await self._storage.pipelines.get(pipeline_id)
        if identity is None or identity.soft_deleted_at is not None:
            return Err(f"Pipeline id {pipeline_id} not found.")

        # snapshot_no is a placeholder (0): the atomic append computes the real
        # next number inside its own transaction (no cross-transaction race).
        incoming = self._build_snapshot(config, pipeline_id, 0, source, note)
        current = await self._storage.snapshots.current(pipeline_id)
        if current is not None and self._same_config(incoming, current):
            return Ok(PipelineMutationResult(pipeline=identity, snapshot=current, created=False, appended=False))

        stored = await self._storage.snapshots.create_and_set_current(pipeline_id, incoming)
        if stored.is_err:
            return Err(stored.unwrap_err())
        head = stored.unwrap()
        updated = (await self._storage.pipelines.get(pipeline_id)) or identity
        return Ok(PipelineMutationResult(pipeline=updated, snapshot=head, created=False, appended=True))

    async def find_by_name(self, name: str) -> Pipeline | None:
        """Return the active identity with ``name``, or ``None``."""
        return await self._storage.pipelines.find_by_name(name)

    async def resolve(self, pipeline_id: int) -> Result[tuple[Pipeline, PipelineSnapshot], str]:
        """Resolve an identity + its current head snapshot for a stage run.

        Error messages match the CLI's originals: an empty store and an unknown
        id produce the exact strings a stage command would surface.
        """
        pipelines = await self._storage.pipelines.list()
        if not pipelines:
            return Err("No stored pipelines. Run `jobfucker init` first.")
        match = next((p for p in pipelines if p.id == pipeline_id), None)
        if match is None:
            return Err(f"Pipeline id {pipeline_id} not found.")
        snapshot = await self._storage.snapshots.current(match.id)
        if snapshot is None:
            return Err(f"Pipeline id {pipeline_id} not found.")
        return Ok((match, snapshot))

    async def snapshots(self, pipeline_id: int) -> Result[list[PipelineSnapshot], str]:
        """Return the snapshot history for ``pipeline_id`` (ordered by snapshot_no)."""
        identity = await self._storage.pipelines.get(pipeline_id)
        if identity is None or identity.soft_deleted_at is not None:
            return Err(f"Pipeline id {pipeline_id} not found.")
        return Ok(await self._storage.snapshots.list(pipeline_id))

    async def list(self) -> list[Pipeline]:
        """Return all active identities, oldest first."""
        return await self._storage.pipelines.list()

    def _build_snapshot(
        self,
        config: PipelineConfig,
        pipeline_id: int,
        snapshot_no: int,
        source: str,
        note: str | None,
    ) -> PipelineSnapshot:
        """Assemble a persistable snapshot DTO for ``config`` under ``pipeline_id``."""
        base = to_pipeline_snapshot(config)
        return replace(
            base,
            pipeline_id=pipeline_id,
            snapshot_no=snapshot_no,
            source=source,
            note=note,
        )

    @staticmethod
    def _same_config(lhs: PipelineSnapshot, rhs: PipelineSnapshot) -> bool:
        """``True`` when two snapshots carry identical config content.

        Ignores identity/bookkeeping fields (id/pipeline_id/snapshot_no/source/
        note/created_at) — those differ between snapshots by construction.
        """
        return all(getattr(lhs, field) == getattr(rhs, field) for field in _SAME_CONFIG_FIELDS)
