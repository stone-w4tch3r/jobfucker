"""Phase 1 (task 1.3): e2e family gate scaffold.

Proves the **real-HH gate is wired and selectable**: this test is ``e2e``-marked
so the root conftest ``pytest_collection_modifyitems`` skip-marks it unless
``--run-e2e`` is passed. Its body only runs with the flag on and asserts the gate
fired — a trivial, network-free placeholder until Phase 8 fills the real HH
end-to-end check (which needs the parallel HH-client track and credentials).
"""

from __future__ import annotations

import pytest


@pytest.mark.e2e
def test_e2e_gate_is_wired_and_selectable(pytestconfig: pytest.Config) -> None:
    """Assert the ``--run-e2e`` gate selected this test.

    Skipped by default; only reachable when the root conftest's e2e skip is
    lifted by ``--run-e2e``, so a passing run here proves the gate is wired.
    """
    assert bool(pytestconfig.getoption("--run-e2e")) is True
