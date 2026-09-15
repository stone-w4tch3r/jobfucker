"""Canonical BDD-step runner for the async domain (async-migration D1).

pytest-bdd 8.x calls step functions via ``_pytest.fixtures.call_fixture_func``,
which never awaits a coroutine — an ``async def`` step body is **silently
dropped** while the scenario still reports PASS (false green). Therefore BDD
steps stay synchronous ``def`` and call the async domain through these helpers,
which run the coroutine to completion on a fresh one-shot loop and unwrap
:class:`Result` centrally.

Rules (test/AGENTS.md after the migration):
- ``async_run`` — for async non-Result callables.
- ``async_run_result`` — for async ``Result``-returning callables: ``Ok`` →
  value, ``Err`` → :func:`pytest.fail` with the error text (an ``Err`` in a
  ``Given`` fails the scenario at the Given).
- Never call either from an already-running loop (qasync thread): BDD uses
  per-call ``asyncio.run``; the Qt GUI uses its persistent qasync loop — the two
  never mix.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import pytest
from rusty_results.prelude import Result

__all__ = ["async_run", "async_run_result"]


def async_run[T](coro: Awaitable[T]) -> T:
    """Run an async, non-Result callable to completion on a fresh loop."""
    return asyncio.run(coro)


def async_run_result[T, E](coro: Awaitable[Result[T, E]]) -> T:
    """Run an async, Result-returning callable; unwrap Ok or fail the test."""
    result = asyncio.run(coro)
    if result.is_err:
        pytest.fail(f"async_result error: {result.unwrap_err()}")
    return result.unwrap()
