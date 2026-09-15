"""Regression tests for the async-step tripwire installed by ``test/conftest.py``.

test/conftest.py patches ``pytest_bdd.given/when/then`` (idempotently, before
step modules import them) so an ``async def`` BDD step fails loudly at
registration instead of silently no-oping (pytest-bdd executes steps via
``call_fixture_func``, which never awaits a coroutine - false green). These
tests pin the guard's contract by exercising the real guard function from
conftest directly.
"""

from __future__ import annotations

import pytest

from test.conftest import guard_step_decorator


def test_guard_rejects_async_step() -> None:
    """Registering an async def step raises a TypeError naming the step kind."""

    async def bad_step() -> None:
        return None

    decorator = guard_step_decorator("then", lambda func: func)
    with pytest.raises(TypeError, match="async def"):
        decorator(bad_step)


def test_guard_passes_sync_step_through() -> None:
    """A plain def step passes through the guard untouched."""

    def good_step() -> None:
        return None

    decorator = guard_step_decorator("then", lambda func: func)
    assert decorator(good_step) is good_step
