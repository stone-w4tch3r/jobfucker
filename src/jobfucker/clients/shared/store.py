"""Atomic, permission-restricted JSON persistence for client session state.

Board clients persist small validated snapshots (auth tokens, cookie jars)
below ``<data_dir>/<service>/<profile_id>/``. :class:`AtomicJsonStore` owns the
write mechanics once: a private directory, an atomic replace, and user-only
file permissions where the platform supports them. The payload model and the
snapshot versioning stay with the board.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel, ValidationError
from rusty_results.prelude import Err, Ok, Result

from jobfucker.clients.base import ClientError, InternalError


class AtomicJsonStore[ModelT: BaseModel]:
    """Atomically persist one validated pydantic snapshot.

    The file lives at ``<data_dir>/<service>/<profile_id>/<filename>``. A
    missing or corrupt file loads as ``None`` (treated as "no state"); a write
    failure is an expected ``Result`` failure, never a raise.
    """

    def __init__(  # noqa: PLR0913 - explicit (location + payload model + error label) configuration
        self,
        *,
        data_dir: Path,
        service: str,
        profile_id: str,
        filename: str,
        model: type[ModelT],
        entity: str,
    ) -> None:
        self._entity = entity
        self._directory = data_dir / service / profile_id
        self._path = self._directory / filename
        self._model: type[ModelT] = model

    @property
    def path(self) -> Path:
        """The absolute path of the persisted snapshot (for diagnostics/tests)."""
        return self._path

    async def load(self) -> ModelT | None:
        """Load the validated snapshot; missing or corrupt state is ``None``."""
        return await asyncio.to_thread(self._load_sync)

    async def save(self, state: ModelT) -> Result[None, ClientError]:
        """Write a complete snapshot atomically with user-only permissions."""
        try:
            await asyncio.to_thread(self._save_sync, state)
        except OSError as exc:
            return Err(InternalError(message=f"Cannot persist {self._entity}: {type(exc).__name__}"))
        return Ok(None)

    def _load_sync(self) -> ModelT | None:
        try:
            raw = self._path.read_text(encoding="utf-8")
            return self._model.model_validate_json(raw)
        except OSError, ValidationError:
            return None

    def _save_sync(self, state: ModelT) -> None:
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._directory.chmod(0o700)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{self._path.name}-", dir=self._directory, text=True)
        temporary_path = Path(temporary_name)
        try:
            # os.fchmod is POSIX-only and missing on Windows. The mode-bit calls
            # are no-ops there and the state file is protected by the default
            # per-user profile ACLs; accepted deliberately.
            if sys.platform != "win32":
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                descriptor = -1
                stream.write(state.model_dump_json())
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self._path)
            self._path.chmod(0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_path.exists():
                temporary_path.unlink()
