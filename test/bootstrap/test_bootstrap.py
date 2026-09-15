"""Bootstrap facade compatibility smoke (Slice B).

Sice B removed ``bootstrap_cli``/``CliServices`` (the CLI-shaped graph was
replaced by the UI-neutral :class:`~jobfucker.app.services.AppServices` via
``AppServices.open()``). This smoke proves the documented surface that survives
the removal is still importable and callable:

- :func:`build_engine_from_pipeline` — the engine-rebuild seam the CLI + the
  gated e2e test depend on;
- :class:`TerminalAuthInteraction` — the CLI auth provider kept in this module.

The real graph/behaviour coverage for the changed seams now lives in
``test/app/``; this file is intentionally minimal (a lint-clean surface guard).
"""

from __future__ import annotations

import jobfucker.bootstrap as b


def test_bootstrap_reexports_build_engine_and_terminal() -> None:
    """``build_engine_from_pipeline`` and ``TerminalAuthInteraction`` still import."""
    assert callable(b.build_engine_from_pipeline)
    assert b.TerminalAuthInteraction is not None
