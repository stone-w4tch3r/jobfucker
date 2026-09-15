"""Phase 4 (task 4.2): runtime isolation directory tests.

Verifies env-var overrides (``CONFIG_DIR`` / ``DATA_DIR`` /
``PLAYWRIGHT_BROWSERS_PATH``), the XDG fallbacks, that ``jobfucker.db``
derives from ``DATA_DIR``, and that ``ensure_runtime_dirs`` creates the dirs.
The ``runtime_dir`` fixture already wires ``CONFIG_DIR``/``DATA_DIR`` for test
isolation; here we drive the resolution logic directly via ``monkeypatch``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobfucker import runtime


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all runtime-related env vars so fallbacks are exercised."""
    for key in ["CONFIG_DIR", "DATA_DIR", "PLAYWRIGHT_BROWSERS_PATH", "XDG_CONFIG_HOME", "XDG_DATA_HOME"]:
        monkeypatch.delenv(key, raising=False)


def test_env_overrides_win(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Explicit env overrides take precedence over XDG/platform defaults."""
    config = tmp_path / "myconfig"
    data = tmp_path / "mydata"
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONFIG_DIR", str(config))
    monkeypatch.setenv("DATA_DIR", str(data))
    assert runtime.config_dir() == config
    assert runtime.data_dir() == data
    assert runtime.database_path() == data / "jobfucker.db"


def test_xdg_fallbacks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """XDG_CONFIG_HOME/XDG_DATA_HOME are used when no direct override is set."""
    _clear_env(monkeypatch)
    xdg_config = tmp_path / "xdg-config"
    xdg_data = tmp_path / "xdg-data"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    assert runtime.config_dir() == xdg_config / "jobfucker"
    assert runtime.data_dir() == xdg_data / "jobfucker"


def test_database_path_derives_from_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``jobfucker.db`` lives directly under ``DATA_DIR``."""
    data = tmp_path / "appdata"
    _clear_env(monkeypatch)
    monkeypatch.setenv("DATA_DIR", str(data))
    assert runtime.database_path() == data / "jobfucker.db"


def test_ensure_runtime_dirs_creates_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``ensure_runtime_dirs`` creates both CONFIG_DIR and DATA_DIR."""
    config = tmp_path / "cfg"
    data = tmp_path / "dat"
    _clear_env(monkeypatch)
    monkeypatch.setenv("CONFIG_DIR", str(config))
    monkeypatch.setenv("DATA_DIR", str(data))
    assert not config.exists()
    assert not data.exists()
    runtime.ensure_runtime_dirs()
    assert config.is_dir()
    assert data.is_dir()
