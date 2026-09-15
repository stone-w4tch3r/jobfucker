"""Unit tests for the canonical BDD-step async bridge (jobfucker.testing.step_runner).

BDD steps stay synchronous and drive the async domain through this bridge
(test/AGENTS.md §6): ``async_run`` completes a plain async callable,
``async_run_result`` unwraps a :class:`Result` (``Ok`` → value, ``Err`` →
``pytest.fail``). These tests pin that contract, including the never-otherwise-
exercised ``Err → pytest.fail`` branch (step_runner.py:40) and exception
propagation through ``asyncio.run``.
"""

from __future__ import annotations

import pytest
from rusty_results.prelude import Err, Ok, Result

from jobfucker.testing.step_runner import async_run, async_run_result


async def _plain(value: int) -> int:
    """Return the value unchanged."""
    return value


async def _boom() -> None:
    """Raise a plain exception (never produces a Result)."""
    raise RuntimeError("boom")


async def _boom_result() -> Result[str, str]:
    """Raise before a Result is produced - tests non-Result exception propagation."""
    raise RuntimeError("boom")


async def _ok_result(value: str) -> Result[str, str]:
    """Return an Ok result."""
    return Ok(value)


async def _err_result() -> Result[str, str]:
    """Return an Err result."""
    return Err("expected failure text")


def test_async_run_returns_value() -> None:
    """async_run completes the coroutine and returns its value."""
    assert async_run(_plain(42)) == 42


def test_async_run_propagates_raised_exception() -> None:
    """async_run surfaces exceptions raised inside the coroutine."""
    with pytest.raises(RuntimeError, match="boom"):
        async_run(_boom())


def test_async_run_result_unwraps_ok() -> None:
    """async_run_result returns the Ok value."""
    assert async_run_result(_ok_result("value")) == "value"


def test_async_run_result_fails_on_err() -> None:
    """async_run_result turns an Err into pytest.fail carrying the error text."""
    with pytest.raises(pytest.fail.Exception, match="async_result error: expected failure text"):
        async_run_result(_err_result())


def test_async_run_result_propagates_non_result_exception() -> None:
    """Exceptions raised before a Result is produced propagate through asyncio.run."""
    with pytest.raises(RuntimeError, match="boom"):
        async_run_result(_boom_result())
