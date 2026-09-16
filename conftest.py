"""Root pytest configuration for the jobfucker suite.

This conftest lives at the repo root so its hooks apply to every collected test
— the ``shared_tests`` building-block suite and the app's own ``test/`` suites
alike. It owns the cross-cutting marker gate (see test/AGENTS.md §7):

- ``--run-e2e``: an **opt-in** flag. ``e2e``-marked tests are skipped unless it
  is passed, so the live-board gate never runs by default.

(The Qt/headless gate — ``QT_QPA_PLATFORM=offscreen`` for ``qt``-marked items —
was removed together with the UI implementation; the rewrite per
``docs/ui/jobfucker.ui.md`` will bring it back.)
"""

from __future__ import annotations

import pytest


# Options are registered on the root conftest so they apply to the whole session
# (both ``shared_tests`` and ``test``), regardless of which conftest file a test
# happens to live under.
def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--run-e2e`` opt-in flag used to gate the e2e family."""
    parser.addoption(
        "--run-e2e",
        action="store_true",
        default=False,
        help="run e2e-marked tests (opt-in gate; skipped by default)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every ``e2e``-marked item unless ``--run-e2e`` was passed.

    This proves the gate is wired and selectable from day one while keeping e2e
    out of the default run.
    """
    run_e2e = bool(config.getoption("--run-e2e"))

    if not run_e2e:
        skip_e2e = pytest.mark.skip(reason="e2e tests are opt-in; pass --run-e2e")
        for item in items:
            if "e2e" in item.keywords:
                item.add_marker(skip_e2e)
