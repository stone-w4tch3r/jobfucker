"""Runtime isolation directories (Phase 4, task 4.2).

Follows architecture §6: the app's state (session cookies, tokens, the
``jobfucker.db`` database) is confined to system-default
directories (``$XDG_CONFIG_HOME``/``$XDG_DATA_HOME`` on Linux, platform-
specific equivalents on macOS/Windows) or explicit env-var overrides. Deleting
a directory resets the state it holds (auth/session for ``DATA_DIR``, config
for ``CONFIG_DIR``).

- ``CONFIG_DIR`` — user configuration files.
- ``DATA_DIR`` — cookies, tokens, ``jobfucker.db`` (the app's single source of
  truth), session state.
Resolution precedence (per directory): explicit env override, then XDG
``$XDG_*_HOME``, then a cross-platform default. This module creates the
directories on demand (``ensure_runtime_dirs``) and exposes the pieces the
composition root (Phase 6) and ``ClientDeps.data_dir`` need.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

# Application-scoped directory names under each platform home.
_CONFIG_DIR_NAME = "jobfucker"
_DATA_DIR_NAME = "jobfucker"


def _platform_default_config_home() -> Path:
    """The platform's config home (``~/.config`` on Linux, AppData elsewhere)."""
    system = platform.system()
    if system == "Windows":
        # %APPDATA% is the roaming app config dir.
        appdata = os.environ.get("APPDATA")
        home = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
        return home
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".config"


def _platform_default_data_home() -> Path:
    """The platform's data home (``~/.local/share`` on Linux, macOS/Windows equivalents)."""
    system = platform.system()
    if system == "Windows":
        # %LOCALAPPDATA% is the non-roaming app data dir.
        local = os.environ.get("LOCALAPPDATA")
        home = Path(local) if local else Path.home() / "AppData" / "Local"
        return home
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support"
    return Path.home() / ".local" / "share"


def config_dir() -> Path:
    """Resolve the config directory (``CONFIG_DIR`` override, then XDG/platform default)."""
    override = os.environ.get("CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / _CONFIG_DIR_NAME
    return _platform_default_config_home() / _CONFIG_DIR_NAME


def data_dir() -> Path:
    """Resolve the data directory (``DATA_DIR`` override, then XDG/platform default)."""
    override = os.environ.get("DATA_DIR")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / _DATA_DIR_NAME
    return _platform_default_data_home() / _DATA_DIR_NAME


def database_path() -> Path:
    """The ``jobfucker.db`` path, derived from :func:`data_dir`."""
    return data_dir() / "jobfucker.db"


def ensure_runtime_dirs() -> None:
    """Create ``CONFIG_DIR`` and ``DATA_DIR`` on disk (idempotent)."""
    config_dir().mkdir(parents=True, exist_ok=True)
    data_dir().mkdir(parents=True, exist_ok=True)


# Re-exported for convenience.
__all__ = [
    "config_dir",
    "data_dir",
    "database_path",
    "ensure_runtime_dirs",
]
