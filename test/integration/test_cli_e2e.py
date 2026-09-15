"""E2E gate scenario — full CLI-to-DB round trip against the mock client.

This is the gated ``e2e`` scenario (plan §7, Task 11). It runs the **real**
``jobfucker`` CLI untouched — via typer's :class:`CliRunner`, no stubbed
commands and no stubbed engine reconstruction — against the first-party mock
client in an isolated ``CONFIG_DIR``/``DATA_DIR``:

    init --config <docs/examples/pipeline.mock.yaml>     # store the pipeline in the real DB
    fetch  --pipeline-id <id>              # no fresh --config: engine rebuilt from the DB row
    score  --pipeline-id <id>
    generate --pipeline-id <id>
    apply  --pipeline-id <id>              # position stages all run from --pipeline-id

``Then`` the CLI wrote 3 vacancies and all of them end ``applied``, proven by
re-opening the real SQLite DB at ``DATA_DIR/jobfucker.db``. This is the
highest-value cross-layer gate: storage migrations → config-from-DB
reconstruction (``build_engine_from_pipeline``) → engine decomposition →
distinct per-stage CLI commands, all without a live board.

Only the **AI client** is stubbed: ``jobfucker.bootstrap.OpenAIWrapper`` is
replaced so the engine never hits live OpenAI API for scoring / cover-letter
generation. Everything else — typer command dispatch, ``AppServices.open``,
``build_engine_from_pipeline`` (real config-from-DB), factories, the mock
client, storage — is real. ``TERM=xterm`` lets the terminal captcha auto-detect
succeed (the generic sixel handler), which is what the no-flag ``score`` /
``generate`` path needs.

Scenario (BDD):
    Given an isolated runtime dir and a stubbed AI
    When the real CLI runs init, fetch, score, generate and apply by --pipeline-id
    Then every stage exits 0 and the real DB holds 3 applied vacancies

Skipped by default (the ``e2e`` marker is gated by ``--run-e2e``); proven under
``uv run poe test --run-e2e``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobfucker.ai import AiClient
from jobfucker.cli import app
from jobfucker.config import OpenAIConfig
from jobfucker.storage.db import open_storage
from jobfucker.testing.step_runner import async_run
from test.pipeline_helpers import ScriptedAi

_MOCK_FETCHED = 3

runner = CliRunner()


def _scripted_ai_factory(_config: OpenAIConfig) -> AiClient:
    """A stand-in for ``OpenAIWrapper`` that never touches the network.

    ``build_engine_from_pipeline`` calls ``OpenAIWrapper(config.openai)``; this
    replaces that constructor so every rebuilt engine gets a fresh always-
    succeeding :class:`ScriptedAi`. The config argument is ignored — the mock
    pipeline is offline.
    """

    return ScriptedAi()


def _invoke_ok(argv: list[str]) -> str:
    """Run one real CLI invocation and return output, asserting exit 0."""
    result = runner.invoke(app, argv)
    assert result.exit_code == 0, f"`{' '.join(argv)}` failed ({result.exit_code}): {result.output}{result.stderr}"
    return result.output


@pytest.mark.e2e
def test_init_then_stages_by_pipeline_id_round_trips_to_db(
    runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_pipeline_yaml: Path,
) -> None:
    """Full CLI-to-DB round trip: init once, then every stage by --pipeline-id."""
    # Terminal auto-detection must succeed for the no-flag score/generate path
    # (they carry no captcha flags), so pin a recognized terminal.
    monkeypatch.setenv("TERM", "xterm")
    # Only the AI is stubbed; the rest of the composition is real.
    monkeypatch.setattr("jobfucker.bootstrap.OpenAIWrapper", _scripted_ai_factory)

    # --- init: store the pipeline in the real (migrated) isolated DB ---------
    init_output = _invoke_ok(["init", "--config", str(mock_pipeline_yaml)])
    match = re.search(r"with id (\d+)", init_output)
    assert match is not None, f"init did not report an id: {init_output!r}"
    pipeline_id = int(match.group(1))

    # --- each stage runs from --pipeline-id with NO fresh --config ------------
    fetch = _invoke_ok(["fetch", "--pipeline-id", str(pipeline_id)])
    assert f"fetch: fetched={_MOCK_FETCHED}" in fetch

    score = _invoke_ok(["score", "--pipeline-id", str(pipeline_id)])
    assert "score: total" in score and "scored=3" in score

    generate = _invoke_ok(["generate", "--pipeline-id", str(pipeline_id)])
    assert "generate-cv:" in generate and "generated=3" in generate

    apply = _invoke_ok(["apply", "--pipeline-id", str(pipeline_id)])
    assert "apply: total" in apply and "applied=3" in apply

    # --- prove the round trip against the real DB in the isolated DATA_DIR ---
    storage = open_storage(runtime_dir / "data" / "jobfucker.db")
    try:
        vacancies = async_run(storage.vacancies.list_by_pipeline(pipeline_id))
        assert len(vacancies) == _MOCK_FETCHED
        assert all(v.apply_status == "applied" for v in vacancies)
    finally:
        async_run(storage.engine.dispose())
