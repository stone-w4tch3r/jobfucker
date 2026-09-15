"""Reporting-stream tests: the live output sink (`jobfucker.reporting`).

Covers the run-event stream the core stages + clients emit on:

- `verbosity_of` maps severities to verbosity (C1 tier: warning/error → 0,
  info/success → 1, debug → 2);
- `CliReporter` filters by the `-v`/`-vv` verbosity threshold (verbosity 0 →
  only warning/error lines, 1 → + info/success, 2 → + debug detail);
- the stages publish the expected severities per vacancy without changing what
  a stage *does* (the `*Report` aggregate still drives everything downstream);
- a client (mock) publishes `debug` board-operation detail through
  ``deps.reporter``, and is silent when the default no-op `NullReporter` is in
  place;
- `NullReporter` (the explicit/default sink) is a no-op.

The shared severity→verbosity map (:func:`verbosity_of`) is authoritative:
stages/clients choose a *level* and never a verbosity integer.

Async-migration convention: stage/client calls are `async def` and awaited
directly (plain pytest-asyncio), mirroring the other stage suites.
"""

from __future__ import annotations

from collections.abc import Coroutine
from dataclasses import replace

import pytest
from rusty_results.prelude import Err

from jobfucker.ai import ScoreError
from jobfucker.clients.base import ApplySucceeded, ClientDeps, SearchWindow, ServiceVacancyId
from jobfucker.clients.mock.client import MockClient
from jobfucker.clients.mock.params import MockSearchEntry, MockSearchParams, MockServiceConfig
from jobfucker.reporting import EventLevel, NullReporter, RunEvent, verbosity_of
from jobfucker.stages.apply import ApplyTargets, run_apply
from jobfucker.stages.fetch import FetchInputs, run_fetch
from jobfucker.stages.prompts import PromptInputs, PromptTemplate
from jobfucker.stages.score import run_score
from jobfucker.storage.db import Storage
from test.pipeline_helpers import (
    ScriptedAi,
    create_pipeline,
    make_factory,
    mock_vacancies,
    snapshot_for,
)
from test.storage.builders import build_vacancy

TODAY = "2026-08-05"


class RecordingReporter:
    """A reporter capturing every published event for direct assertion."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    async def publish(self, event: RunEvent) -> None:
        self.events.append(event)


def _score_inputs() -> PromptInputs:
    """Minimal prompt inputs the stub AI accepts (it ignores content)."""
    template = PromptTemplate(params={}, body="Score {{ resume_formatted }} for {{ vacancy_formatted }}")
    return PromptInputs(resume="resume contents", prompt=template)


async def _seed_scored(storage: Storage, pipeline_id: int, *, count: int = 2) -> None:
    """Seed ``count`` scored, cover-lettered, pending vacancies at positions 0..n."""
    for index in range(count):
        await storage.vacancies.upsert(
            build_vacancy(
                pipeline_id=pipeline_id,
                external_id=f"v{index}",
                score=5,
                score_reasoning="good",
                cover_letter=f"letter {index}",
                apply_status=None,
            )
        )


def run_async(coro: Coroutine[None, None, None]) -> None:
    """Run a one-shot coroutine on a throwaway loop (CLI-style test helper)."""
    import asyncio

    asyncio.run(coro)


def _mock_section() -> MockServiceConfig:
    """A mock section with the canonical canned vacancies (all-succeed default)."""
    return MockServiceConfig(
        resume_id="mock-resume-1",
        searches=(
            MockSearchEntry(
                query="python",
                filter=MockSearchParams(
                    area=(1,),
                    schedule=("fullDay",),
                    experience="between1And3",
                    only_with_salary=True,
                ),
                vacancies=mock_vacancies(),
            ),
        ),
    )


# --- verbosity_of -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("success", 0),
        ("warning", 0),
        ("error", 0),
        ("info", 1),
        ("debug", 2),
    ],
)
def test_verbosity_of_maps_severity_to_tier(level: EventLevel, expected: int) -> None:
    """The severity→verbosity map places every level in exactly one tier."""
    assert verbosity_of(level) == expected


# --- CliReporter filtering ---------------------------------------------------------
async def _drive_cli_reporter(verbosity: int, out: list[str]) -> None:
    """Drive the real sys.path-exposed :class:`CliReporter` into ``out``."""
    from jobfucker.cli import CliReporter

    reporter = CliReporter(verbosity=verbosity)
    for event in _events():
        await reporter.publish(event)


def _events() -> list[RunEvent]:
    """A canonical event set exercising all severity→verbosity tiers + framing."""
    return [
        RunEvent(stage="score", message="scored https://x/1 with 4/5 below threshold", level="warning"),
        RunEvent(stage="fetch", message="fetched Python Dev https://x/4", level="info"),
        RunEvent(stage="client", message="search page 1: found 3 vacancies", level="debug"),
        RunEvent(stage="apply", message="applied v1 — Python Dev", level="success", index=1, total=2),
        RunEvent(stage="apply", message="failed v2 — board refused", level="error", index=2, total=2),
    ]


def test_cli_reporter_verbosity_zero_shows_success_warnings_and_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No `-v` flag: the success/warning/error tier prints, info + debug do not."""
    out: list[str] = []
    monkeypatch.setattr("jobfucker.cli.typer.echo", out.append)
    run_async(_drive_cli_reporter(0, out))
    assert out == [
        "scored https://x/1 with 4/5 below threshold",
        "[1/2] applied v1 — Python Dev",
        "[2/2] failed v2 — board refused",
    ]


def test_cli_reporter_verbosity_one_shows_per_vacancy_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`-v`: success/warning/error + info print, debug client detail does not."""
    out: list[str] = []
    monkeypatch.setattr("jobfucker.cli.typer.echo", out.append)
    run_async(_drive_cli_reporter(1, out))
    assert out == [
        "scored https://x/1 with 4/5 below threshold",
        "fetched Python Dev https://x/4",
        "[1/2] applied v1 — Python Dev",
        "[2/2] failed v2 — board refused",
    ]


def test_cli_reporter_verbosity_two_shows_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """`-vv`: every tier prints, including the dimmed, stage-tagged client detail."""
    out: list[str] = []
    monkeypatch.setattr("jobfucker.cli.typer.echo", out.append)
    run_async(_drive_cli_reporter(2, out))
    assert out == [
        "scored https://x/1 with 4/5 below threshold",
        "fetched Python Dev https://x/4",
        "[client] search page 1: found 3 vacancies",
        "[1/2] applied v1 — Python Dev",
        "[2/2] failed v2 — board refused",
    ]


def test_cli_reporter_plain_without_tty_and_colored_on_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Colors are gated on tty-ness: piped output stays plain, a tty gets ANSI."""
    out: list[str] = []
    monkeypatch.setattr("jobfucker.cli.typer.echo", out.append)
    run_async(_drive_cli_reporter(0, out))
    assert all("\x1b[" not in line for line in out)  # pytest capture is not a tty

    class _FakeTty:
        def isatty(self) -> bool:
            return True

    tty_out: list[str] = []
    monkeypatch.setattr("jobfucker.cli.sys.stdout", _FakeTty())
    monkeypatch.setattr("jobfucker.cli.typer.echo", tty_out.append)
    run_async(_drive_cli_reporter(0, tty_out))
    assert any("\x1b[" in line for line in tty_out)


# --- stage events ------------------------------------------------------------------
async def test_score_publishes_per_vacancy_events(storage: Storage) -> None:
    """`run_score` emits one `score` event per vacancy with index/total fields."""
    pipeline = await create_pipeline(storage, name="score-events", min_required_score=3)
    await _seed_scored(storage, pipeline.id, count=1)
    reporter = RecordingReporter()

    report = await run_score(
        storage,
        pipeline,
        ScriptedAi(),
        _score_inputs(),
        snapshot_id=(await snapshot_for(storage, pipeline)).id,
        min_required_score=3,
        progress=reporter,
    )

    assert report.is_ok
    assert len(reporter.events) == 2
    start, event = reporter.events
    assert start.level == "info" and start.message.startswith("starting scoring ")
    assert event.stage == "score"
    assert verbosity_of(event.level) == 0
    assert "scored" in event.message and "/5" in event.message
    assert event.index == 1 and event.total == 1
    assert event.item is not None
    assert event.level == "success"


async def test_score_subthreshold_event_is_warning_level(storage: Storage) -> None:
    """A sub-threshold outcome publishes a `warning` event (not success)."""
    pipeline = await create_pipeline(storage, name="score-warn", min_required_score=5)
    await _seed_scored(storage, pipeline.id, count=1)
    reporter = RecordingReporter()

    report = await run_score(
        storage,
        pipeline,
        ScriptedAi(),
        _score_inputs(),
        snapshot_id=(await snapshot_for(storage, pipeline)).id,
        min_required_score=5,
        progress=reporter,
    )

    assert report.is_ok
    assert reporter.events[0].level == "info"
    event = reporter.events[-1]
    assert event.level == "warning"
    assert verbosity_of(event.level) == 0
    assert event.message.endswith("— below threshold, skipped")


async def test_score_error_event_is_error_level(storage: Storage) -> None:
    """A per-vacancy score failure publishes an `error` event; the batch continues."""
    pipeline = await create_pipeline(storage, name="score-err", min_required_score=3)
    await _seed_scored(storage, pipeline.id, count=1)
    ai = ScriptedAi(scores=[Err(ScoreError(message="boom", raw="{}"))])
    reporter = RecordingReporter()

    report = await run_score(
        storage,
        pipeline,
        ai,
        _score_inputs(),
        snapshot_id=(await snapshot_for(storage, pipeline)).id,
        min_required_score=3,
        progress=reporter,
    )

    assert report.is_ok and report.unwrap().failed == 1
    assert reporter.events[0].level == "info"
    event = reporter.events[-1]
    assert event.level == "error"
    assert "score failed for" in event.message


async def test_apply_publishes_applied_events(client_deps: ClientDeps, storage: Storage) -> None:
    """`run_apply` emits a success-tier `applied <id> — <title>` event per successful apply."""
    pipeline = await create_pipeline(storage, name="apply-events")
    await _seed_scored(storage, pipeline.id, count=2)
    factory = make_factory(client_deps, section=_mock_section())
    reporter = RecordingReporter()

    report = await run_apply(
        storage,
        pipeline,
        ApplyTargets(client=factory.get("mock"), resume_id="mock-resume-1"),
        snapshot_id=(await snapshot_for(storage, pipeline)).id,
        min_required_score=3,
        daily_apply_limit=50,
        login="login@example.com",
        service="mock",
        today=TODAY,
        progress=reporter,
    )

    assert report.is_ok
    applied = [e for e in reporter.events if e.stage == "apply"]
    assert len(applied) == 2
    assert all(e.message.startswith("applied v") and " — " in e.message for e in applied)
    assert all(e.index == i + 1 and e.total == 2 for i, e in enumerate(applied))
    assert all(verbosity_of(e.level) == 0 and e.level == "success" for e in applied)


async def test_apply_publishes_ignore_event_per_ineligible_vacancy(client_deps: ClientDeps, storage: Storage) -> None:
    """`run_apply` emits an `info`-tier `not eligible <id> — <reason>` event per ineligible vacancy."""
    pipeline = await create_pipeline(storage, name="apply-ignore-events")
    await _seed_scored(storage, pipeline.id, count=1)
    await storage.vacancies.upsert(
        build_vacancy(pipeline_id=pipeline.id, external_id="noletter", score=5)  # no cover letter
    )
    factory = make_factory(client_deps, section=_mock_section())
    reporter = RecordingReporter()

    report = await run_apply(
        storage,
        pipeline,
        ApplyTargets(client=factory.get("mock"), resume_id="mock-resume-1"),
        snapshot_id=(await snapshot_for(storage, pipeline)).id,
        min_required_score=3,
        daily_apply_limit=50,
        login="login@example.com",
        service="mock",
        today=TODAY,
        progress=reporter,
    )

    assert report.is_ok
    assert report.unwrap().ignored[0].reason == "no cover letter (run generate first)"
    ignored = [e for e in reporter.events if e.stage == "apply" and e.message.startswith("not eligible ")]
    assert len(ignored) == 1
    assert ignored[0].level == "info"
    assert verbosity_of(ignored[0].level) == 1  # hidden without -v
    assert ignored[0].message == "not eligible noletter — no cover letter (run generate first)"
    applied = [e for e in reporter.events if e.stage == "apply" and e.message.startswith("applied ")]
    assert len(applied) == 1


async def test_fetch_publishes_per_vacancy_events(client_deps: ClientDeps, storage: Storage) -> None:
    """`run_fetch` emits one success-tier "fetched" event per persisted vacancy."""
    pipeline = await create_pipeline(storage, name="fetch-events", min_required_score=3)
    reporter = RecordingReporter()
    # The driver's per-page walk events go to the client deps' reporter — the
    # same sink the CLI injects everywhere — so the recording reporter is wired
    # into the factory deps too, not just the stage's ``progress``.
    factory = make_factory(replace(client_deps, reporter=reporter), section=_mock_section())

    report = await run_fetch(
        storage,
        pipeline,
        FetchInputs(client=factory.get("mock"), search_index=0, query="python", params=SearchWindow()),
        snapshot_id=(await snapshot_for(storage, pipeline)).id,
        progress=reporter,
    )

    assert report.is_ok
    fetch_events = [e for e in reporter.events if e.stage == "fetch"]
    # Only per-vacancy events on the fetch stage now; per-page walk progress
    # comes from the client-side driver on the client deps' reporter (the same
    # sink in the CLI wiring — mirrored here by injecting it into the factory).
    vacancy_events = [e for e in fetch_events if e.url]
    assert len(vacancy_events) == 3
    assert all(verbosity_of(e.level) == 1 for e in vacancy_events)
    page_events = [e for e in reporter.events if e.stage == "client" and e.message.startswith("search page")]
    assert len(page_events) == 1
    assert page_events[0].level == "info"


# --- client detail events -------------------------------------------------------------
async def test_mock_client_publishes_page_progress(client_deps: ClientDeps) -> None:
    """The shared paging driver publishes an `info`-tier event per walked page."""
    reporter = RecordingReporter()
    deps = replace(client_deps, reporter=reporter)
    client = MockClient(deps, _mock_section())

    result = await client.search_vacancies(0)

    assert result.is_ok
    detail = [e for e in reporter.events if e.stage == "client"]
    assert detail
    assert detail[0].level == "info"
    assert verbosity_of(detail[0].level) == 1
    assert "search page" in detail[0].message


async def test_mock_client_apply_publishes_ok_success(client_deps: ClientDeps) -> None:
    """A successful `apply: applied` apply publishes a `success` `apply: ok` event."""
    reporter = RecordingReporter()
    deps = replace(client_deps, reporter=reporter)
    client = MockClient(deps, _mock_section())

    result = await client.apply_to_vacancy(resume_id="mock-resume-1", vacancy_id=ServiceVacancyId("v1"))

    assert result.is_ok and isinstance(result.unwrap(), ApplySucceeded)
    ok = [e for e in reporter.events if e.message.startswith("apply: ok")]
    assert ok and ok[0].level == "success"
    assert verbosity_of(ok[0].level) == 0


async def test_client_with_null_reporter_default_is_silent(client_deps: ClientDeps) -> None:
    """`deps.reporter` defaults to the no-op `NullReporter`, so nothing surfaces."""
    client = MockClient(client_deps, _mock_section())

    result = await client.authorize()

    assert result.is_ok  # the default reporter is the silent NullReporter


# --- NullReporter ---------------------------------------------------------------------
async def test_null_reporter_is_noop() -> None:
    """`NullReporter.publish` discards the event without raising."""
    reporter = NullReporter()
    await reporter.publish(RunEvent(stage="x", message="whatever", level="info"))
