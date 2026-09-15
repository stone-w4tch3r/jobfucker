"""Phase 4 (task 4.3): plain pytest tests for config-time cap validation.

Validates that ``daily_apply_limit`` is checked against the client's declared
``per_auth_daily_cap`` (via ``service_info``) at config time, with no cap
constants in core. The behavioral over-cap scenario lives in
``bdd/config.feature``; these unit tests cover the pure logic including the
happy path. Registered mock cap is 200.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from rusty_results.prelude import Ok, Result

from jobfucker.clients.base import ClientCredentials, ClientDeps
from jobfucker.clients.factory import Factory
from jobfucker.clients.mock.client import MockClient
from jobfucker.config import PipelineConfig, load_pipeline_config, validate_cap
from test.config.helpers import build_valid_pipeline_yaml

_MOCK_CAP = 200


def _factory(data_dir: Path) -> Factory:
    """A mock-registered factory for cap validation."""
    deps = ClientDeps(
        service="mock",
        profile_id=None,
        data_dir=data_dir,
        credentials=ClientCredentials(login="test-login", password="test-password"),
        auth_interaction=_StubAuth(),
        captcha_handler=_stub_captcha,
    )
    # The factory is section-less: ``validate_cap`` only reads the cap (static
    # class metadata) via ``Factory.cap`` and never constructs a client.
    factory = Factory(deps)
    factory.register("mock", MockClient)
    return factory


def _load(runtime_dir: Path, limit: int) -> PipelineConfig:
    path = build_valid_pipeline_yaml(runtime_dir / "config", daily_apply_limit=limit)
    result = load_pipeline_config(path)
    assert result.is_ok, f"expected Ok, got {result}"
    return result.unwrap()


def test_limit_below_cap_is_ok(runtime_dir: Path) -> None:
    """A limit under the mock cap validates cleanly."""
    config = _load(runtime_dir, limit=50)
    result = validate_cap(config, _factory(runtime_dir / "data"))
    assert result.is_ok, result.unwrap_err()


def test_limit_equal_to_cap_is_ok(runtime_dir: Path) -> None:
    """A limit exactly at the cap is allowed (boundary inclusive)."""
    config = _load(runtime_dir, limit=_MOCK_CAP)
    result = validate_cap(config, _factory(runtime_dir / "data"))
    assert result.is_ok, result.unwrap_err()


def test_limit_above_cap_is_err(runtime_dir: Path) -> None:
    """A limit above the cap surfaces an Err naming both values."""
    config = _load(runtime_dir, limit=_MOCK_CAP + 1)
    result = validate_cap(config, _factory(runtime_dir / "data"))
    assert result.is_err
    message = result.unwrap_err()
    assert "per-auth daily cap" in message
    assert str(_MOCK_CAP + 1) in message
    assert str(_MOCK_CAP) in message


def test_unknown_service_is_err(runtime_dir: Path) -> None:
    """An unresolvable service surfaces an Err (not a crash)."""
    config = _load(runtime_dir, limit=5)
    # Use a service name that was never registered.
    broken = config.model_copy(update={"service": "nope"})
    result = validate_cap(broken, _factory(runtime_dir / "data"))
    assert result.is_err
    assert "Unknown service" in result.unwrap_err()


def test_validate_cap_uses_cap_not_get(runtime_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``validate_cap`` reads the cap via ``Factory.cap``, never constructing a client.

    The cap is static client metadata resolved from the registration alone; a
    live client must never be built just to check a config-time limit. Guard the
    mock constructor so any construction attempt fails the test loudly.
    """
    config = _load(runtime_dir, limit=50)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("validate_cap must not construct a MockClient")

    monkeypatch.setattr(MockClient, "__init__", _raise)
    result = validate_cap(config, _factory(runtime_dir / "data"))
    assert result.is_ok, result.unwrap_err()


class _StubAuth:
    """Minimal :class:`AuthInteractionProvider` for building a mock factory."""

    async def request_code(self, *, prompt: str) -> Result[str, str]:
        del prompt
        return Ok("123456")

    async def confirm(self, *, prompt: str) -> Result[bool, str]:
        del prompt
        confirmed: bool = True
        return Ok(confirmed)


async def _stub_captcha(image: bytes) -> Result[str, str]:
    del image
    return Ok("stub")
