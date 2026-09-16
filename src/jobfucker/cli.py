"""Typer CLI: thin presentation shell over ``AppServices`` + engine-build.

This module is deliberately **thin** — it parses arguments, delegates pipeline
lifecycle to :class:`~jobfucker.app.pipeline_service.PipelineService` via the
UI-neutral :class:`~jobfucker.app.services.AppServices` graph (opened and
closed by the :func:`_with_services` decorator, which injects the graph as the
command's first argument), rebuilds an :class:`Engine` from the stored row via
:func:`jobfucker.bootstrap.build_engine_from_pipeline` (never a fresh
``--config``), formats output, and maps any top-level ``Err`` to a user message
+ non-zero exit. The shared service graph and config↔storage mapping live in
the ``app/`` layer — no business logic lives here; the engine core stays
typer-free so any presentation layer (a GUI, planned per
``docs/ui/jobfucker.ui.md``) can share it.

**Async boundary (async-migration spec D5):** every command wraps its async
core work in ``asyncio.run(...)`` — a one-shot throwaway loop per command. The
persistent loop exists only in the GUI (removed from this tree; planned for
re-implementation from scratch per ``docs/ui/jobfucker.ui.md``). Config file
reads at ``init``/``update`` are synchronous ``load_pipeline_config`` calls that
run **inside** that per-command loop (the CLI loop is throwaway with no
concurrent tasks, so the blocking read is harmless there). Each command's
:class:`AppServices` graph is closed in a ``finally`` (:func:`_with_services`),
so an owned file-backed engine is disposed before the loop closes (the
``storage.db`` invariant).

Command set (architecture §1.0): ``init``, ``update``, ``run``, ``fetch``,
``score``, ``generate``, ``apply``, ``status``, ``pipelines-list``.

- ``init``/``update`` take ``--config <pipeline.yaml>`` **only** (they never
  construct a client, so they carry no captcha flags). ``init`` is get-or-keep
  (creates on first run, reuses the same-name identity on re-runs); ``update``
  appends a new config snapshot under the existing identity (the id never
  changes, nothing is soft-deleted) and reports ``unchanged`` on a no-op.
- ``fetch`` takes ``--pipeline-id`` (required) + captcha flags + ``--refresh``
  (insert-only mirror sync by default; ``--refresh`` overwrites listing fields
  only, never derived state).
- ``score``/``generate`` take ``--pipeline-id`` (required) + batch flags only.
- ``apply`` takes ``--pipeline-id`` (required) + batch + captcha flags, plus
  per-run eligibility knobs (``--min-score``/``--include-unscored``/
  ``--allow-without-letter``) and forced-ids mode (``--vacancy-id``/``--force``).
- ``run`` takes **exactly one** of ``--pipeline-id`` or ``--new-from-config`` +
  batch + captcha flags.
- Read-only commands (``status``/``pipelines-list``) need no config/id.

**Global logging options** (place before the command, e.g.
``jobfucker --log-level debug score ...``): every invocation wires file logging
(``<DATA_DIR>/logs/jobfucker.log``) at the ``--log-level`` (default ``info``;
``debug`` captures the AI request/response exchange);
``--log-stderr`` mirrors the same level to a colored stderr console (never
stdout — the user interface).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable, Mapping
from enum import StrEnum
from functools import wraps
from pathlib import Path
from typing import Concatenate, Final, Literal, NoReturn

import typer
import yaml
from rusty_results.prelude import Err, Ok, Result

from jobfucker.app.search_view import PreviewContext, build_preview, preview_document, render_text_preview
from jobfucker.app.services import AppServices
from jobfucker.bootstrap import build_client_from_pipeline, build_config_from_pipeline, build_engine_from_pipeline
from jobfucker.cli_commands import vacancies as vacancy_commands
from jobfucker.clients.base import SearchWindow
from jobfucker.config import PipelineConfig, load_pipeline_config, search_index_range_error
from jobfucker.engine import BatchSelector
from jobfucker.hh_tests.dump import dump_problems_to_json
from jobfucker.reporting import EventLevel, RunEvent, verbosity_of
from jobfucker.runtime import data_dir
from jobfucker.stages.apply import ApplyFilters, ApplyReport
from jobfucker.stages.fetch import FetchReport
from jobfucker.stages.fetch_plan import plan_fetch
from jobfucker.stages.generate_cv import GenerateCvReport
from jobfucker.stages.score import ScoreReport
from jobfucker.storage.dto import VacancyRecord
from jobfucker.table_documents.codec import DocumentFormat

app = typer.Typer(
    add_completion=False,
    help="jobfucker — vacancy application automation engine (CLI).",
    context_settings={"help_option_names": ["-h", "--help"]},
)

_FILTERING_HELP = vacancy_commands.FILTERING_HELP

vacancies_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help=f"Review and edit stored vacancies through YAML/JSON documents.\n\n{_FILTERING_HELP}",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(vacancies_app, name="vacancies")

hh_tests_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="HH screening tests: dump test problems for the async solve flow.",
    context_settings={"help_option_names": ["-h", "--help"]},
)
app.add_typer(hh_tests_app, name="hh-tests")

__all__ = ["app", "main"]


# The console log levels accepted by ``--log-level`` (the file log is always
# DEBUG — the durable record — so this only gates the optional stderr console).
_LOG_LEVELS: Final = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


# PEP 695 aliases for the YAML payload boundaries (the restricted-object linter checks
# annotations, not type aliases; payloads are handed to with_overrides untouched).
type _StrKeyedMapping = Mapping[str, object] | None
type _FilterOverride = Mapping[str, object] | None


class SearchFormat(StrEnum):
    """Output formats of the ``search`` preview command."""

    TEXT = "text"
    JSON = "json"
    YAML = "yaml"


def _parse_params_yaml(text: str) -> object:  # lint-ignore[restricted-object]: PyYAML boundary; narrowed below
    """Parse YAML/JSON text into an ``object`` (dynamic boundary)."""
    return yaml.safe_load(text)  # type: ignore[reportAny]  # rationale: PyYAML returns Any; object is the dynamic boundary


def _as_str_keyed_mapping(
    value: object,  # lint-ignore[restricted-object]: YAML boundary
) -> _StrKeyedMapping:
    """Return ``value`` as a str-keyed mapping, or ``None`` if it is not one."""
    if not isinstance(value, dict):
        return None
    narrowed: dict[str, object] = {}  # lint-ignore[restricted-object]: YAML  # lint-ignore[raw-dict]: YAML
    for key in value:  # type: ignore[reportUnknownVariableType]  # rationale: dict from dynamic YAML; keys narrowed to str below
        if isinstance(key, str):
            narrowed[key] = value[key]
    if len(narrowed) != len(value):  # type: ignore[reportUnknownArgument]  # rationale: dynamic YAML dict; only the count matters here
        return None  # some keys were non-strings
    return narrowed


def _load_params_override(
    value: str,
) -> Result[tuple[Mapping[str, object], str], str]:  # lint-ignore[restricted-object]: YAML payload boundary
    """Resolve ``--params`` into (board filter mapping, source label): file, stdin, or inline JSON/YAML.

    ``-`` reads stdin; a value starting with ``{`` or ``[`` is an inline
    YAML/JSON document (a filter document is always a ``{...}`` mapping, so a
    non-mapping inline doc fails with a targeted message instead of a
    confusing file-not-found); anything else is a file path. One YAML loader
    parses all three forms (JSON is a YAML subset). The source label doubles
    as the header echo so the display can never drift from what was parsed.
    """
    if value == "-":
        raw = sys.stdin.read()
        source = "stdin"
    elif value.lstrip().startswith(("{", "[")):
        raw = value
        source = "inline"
    else:
        try:
            raw = Path(value).expanduser().read_text(encoding="utf-8")
        except OSError as exc:
            return Err(f"Cannot read --params file {value!r}: {exc}")
        source = value
    try:
        loaded = _parse_params_yaml(raw)
    except yaml.YAMLError as exc:
        return Err(f"Invalid YAML in --params ({source}): {exc}")
    mapping = _as_str_keyed_mapping(loaded)
    if mapping is None:
        return Err(f"--params ({source}) must be a mapping with string keys (the board's filter block)")
    return Ok((mapping, source))


@app.callback()
def _cli_logging(  # type: ignore[reportUnusedFunction]  # rationale: registered as the typer global callback via @app.callback
    log_stderr: bool = typer.Option(
        False,
        "--log-stderr",
        help="Print colored logs to stderr (place before the command; stdout stays the user interface)",
    ),
    log_level: str = typer.Option(
        "info",
        "--log-level",
        help="Log level for the file log and (with --log-stderr) the stderr console: debug|info|warning|error",
    ),
) -> None:
    """Wire logging for every CLI invocation: file log always, console on demand.

    File logging is always on (``<DATA_DIR>/logs/jobfucker.log``;
    setting-up-logging). The default level is INFO; ``--log-level`` overrides it
    for the file log — ``debug`` captures the AI request/response exchange from
    :mod:`jobfucker.ai` — and, when ``--log-stderr`` is also given, for the
    colored stderr console (never stdout — the CLI user interface). Noisy
    third-party loggers (openai, httpx/httpx2, httpcore/httpcore2, aiosqlite,
    asyncio, PIL) are pinned at WARNING so the logs show only the app's own
    output; set NOT_SILENCE_DEPENDENCIES_LOGS=1 to keep them.
    """
    from jobfucker.shared.logging import setup_file_logging, setup_stdout_logging, silence_noisy_loggers

    level = _LOG_LEVELS.get(log_level.lower())
    if level is None:
        _fail(f"Unknown --log-level {log_level!r}; use one of: debug, info, warning, error")
    setup_file_logging(log_dir=data_dir() / "logs", app_name="jobfucker", level=level)
    silence_noisy_loggers()
    if log_stderr:
        setup_stdout_logging(level=level, stream=sys.stderr)


def _run[T](coro: Awaitable[T]) -> T:
    """Run an async CLI core call to completion on a one-shot loop.

    One-shot throwaway loop per command (D5); never a live loop thread, so
    ``asyncio.run`` is always safe here.
    """
    return asyncio.run(coro)


# --- shared helpers ---------------------------------------------------------
def _fail(message: str) -> NoReturn:
    """Print an error to stderr and exit non-zero (the top-level error boundary)."""
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


def _load_config(path: Path) -> PipelineConfig:
    """Load + validate ``pipeline.yaml``, failing fast with a message + exit 1.

    Synchronous by design: config reads are one-shot setup that runs **inside**
    the per-command ``asyncio.run`` loop (the CLI loop is throwaway with no
    concurrent tasks, so the blocking read is harmless there).
    """
    result = load_pipeline_config(path)
    if result.is_err:
        _fail(f"Failed to load config {path}: {result.unwrap_err()}")
    return result.unwrap()


def _colorize(text: str, level: EventLevel) -> str:
    """tty-only ANSI styling per event severity (colors themselves gated by the caller)."""
    if level == "success":
        return typer.style(text, fg="green")
    if level == "warning":
        return typer.style(text, fg="yellow")
    if level == "error":
        return typer.style(text, fg="red")
    if level == "info":
        return typer.style(text, fg="cyan")
    return typer.style(text, dim=True)


def _render_event(event: RunEvent, *, color: bool) -> str:
    """One console line for a run event: framing + optional styling.

    The stage builds the human message; the CLI adds the ``[i/n]`` position
    frame from the event's index/total fields and renders ``debug``-tier detail
    dimmed with its stage tag (opt-in via ``-vv``). Colors apply only on a tty,
    so piped/redirected output stays plain.
    """
    if event.level == "debug":
        text = f"[{event.stage}] {event.message}"
    elif event.index is not None and event.total is not None:
        text = f"[{event.index}/{event.total}] {event.message}"
    else:
        text = event.message
    return _colorize(text, event.level) if color else text


class CliReporter:
    """The live run-event sink: prints each event as it arrives.

    Filters by the ``-v``/``-vv`` verbosity level via :func:`verbosity_of`
    (the shared severity→verbosity map): verbosity ``0`` (no flag) prints the
    ``success``/``warning``/``error`` events that always matter, ``1`` (``-v``)
    adds the ``info`` lines, ``2`` (``-vv``) also shows the client's
    ``debug``-level board-operation detail. Each printed line is framed by
    :func:`_render_event` (``[i/n]`` progress, severity colors on a tty, dimmed
    client detail). The final aggregate ``*Report`` lines are printed by the
    command regardless of verbosity. Reporters never raise — a broken event is
    dropped rather than failing.
    """

    def __init__(self, *, verbosity: int = 0) -> None:
        self._verbosity = verbosity

    async def publish(self, event: RunEvent) -> None:
        """Echo the event when its severity is within the verbosity threshold."""
        if verbosity_of(event.level) > self._verbosity:
            return
        typer.echo(_render_event(event, color=sys.stdout.isatty()))


def _with_services[**P, R](
    func: Callable[Concatenate[AppServices, P], Awaitable[R]],
) -> Callable[P, Awaitable[R]]:
    """Open :class:`AppServices`, inject it as the first argument, always close it.

    Wraps a command coroutine whose first parameter is the service graph: the
    graph is opened on the command's one-shot loop, passed in, and closed in a
    ``finally`` — including when the command fails via ``_fail`` (which raises
    ``typer.Exit``) — so an owned file-backed engine is disposed before the
    loop closes (the ``storage.db`` invariant).
    """

    @wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        result = await AppServices.open()
        if result.is_err:
            _fail(result.unwrap_err())
        svc = result.unwrap()
        try:
            return await func(svc, *args, **kwargs)
        finally:
            await svc.close()

    return wrapper


def _make_selector(
    from_: int | None,
    to: int | None,
    take: int | None,
    skip_already_processed: bool,
) -> BatchSelector:
    """Build and validate a batch selector, failing fast on bad arguments."""
    selector = BatchSelector(from_=from_, to=to, take=take, skip_already_processed=skip_already_processed)
    validated = selector.validate()
    if validated.is_err:
        _fail(validated.unwrap_err())
    return selector


# --- report formatting ------------------------------------------------------
def _print_fetch(reports: tuple[FetchReport, ...]) -> None:
    """Print one aggregate line per executed search entry, in pool order."""
    total = len(reports)
    for report in reports:
        position = f"{report.search_index + 1}/{total}" if report.search_index < total else str(report.search_index)
        typer.echo(
            f"fetch: search {position} ({report.query!r}): "
            f"fetched={report.fetched}/{report.requested} pages={report.pages} "
            f"already_stored={report.already_stored}"
        )
        if report.exhausted:
            typer.echo("  listing ended before the slice was filled")


def _print_score(report: ScoreReport) -> None:
    """Print a :class:`ScoreReport`'s aggregate line + per-vacancy error rows."""
    typer.echo(
        f"score: total={report.total} scored={report.scored} "
        f"sub_threshold={report.sub_threshold} failed={report.failed}"
    )
    for row in report.vacancies:
        if row.score_error is not None:
            typer.echo(f"  {row.external_id}: error {row.score_error}")


def _print_generate(report: GenerateCvReport) -> None:
    """Print a :class:`GenerateCvReport`'s aggregate line + error rows."""
    typer.echo(
        f"generate-cv: total={report.total} generated={report.generated} "
        f"failed={report.failed} skipped={report.skipped}"
    )
    for row in report.vacancies:
        if row.cover_letter_error is not None:
            typer.echo(f"  {row.external_id}: error {row.cover_letter_error}")


def _print_apply(report: ApplyReport) -> None:
    """Print an :class:`ApplyReport` as the run's ledger.

    Partition invariant: attempted (applied + declined + failed) + not
    attempted (pending) + not eligible (ignored) = ``checked`` — the head line
    names every bucket, so it always sums to the window. Facts have one home:
    declined/failed details live only in the live stream (warning/error tiers
    always print); not-eligible rows live only in the ``-v`` live stream (their
    events are info-tier) — the summary carries just their count.
    """
    if report.checked == 0:
        typer.echo("apply: pipeline has no vacancies")
        return
    tail = f", {len(report.ignored)} not eligible" if report.ignored else ""
    attempted = report.applied + report.skipped + report.failed
    if attempted == 0 and report.pending == 0:
        # The whole window was filtered out before attempts.
        typer.echo(f"apply: {report.checked} in this run — nothing attempted{tail}")
    else:
        # Uniform counts head (zeros included) whenever anything was attempted
        # or left pending.
        head = (
            f"apply: {report.checked} in this run — {report.applied} applied, "
            f"{report.skipped} declined, {report.failed} failed"
        )
        if report.pending > 0:
            # The stop wording is a limit exactly when the stage said so;
            # fatal stops (config/auth) print their own reason in the stream.
            limit_stop = report.stop_message is not None and "daily limit" in report.stop_message
            head += f", {report.pending} not attempted" + (" (limit)" if limit_stop else "")
        typer.echo(head + tail)


@vacancies_app.command(name="dump", epilog=_FILTERING_HELP)
def vacancies_dump(
    output: Path | None = typer.Option(None, "--output", help="Optional destination .yaml, .yml, or .json path"),
    document_format: DocumentFormat | None = typer.Option(
        None,
        "--format",
        help="Output format (default: yaml; inferred from an output file suffix)",
    ),
    force: bool = typer.Option(False, "--force", "-f", help="Replace an existing output file"),
    pipeline_id: int | None = typer.Option(
        None,
        "--pipeline-id",
        help="Only dump vacancies of this pipeline (default: all pipelines)",
    ),
) -> None:
    """Dump every active and soft-deleted vacancy (optionally one pipeline)."""
    vacancy_commands.dump(output=output, document_format=document_format, force=force, pipeline_id=pipeline_id)


@vacancies_app.command(name="apply", epilog=_FILTERING_HELP)
def vacancies_apply(
    source: Path | None = typer.Option(
        None,
        "--source",
        help="Optional source .yaml, .yml, or .json path; stdin otherwise",
    ),
    document_format: DocumentFormat | None = typer.Option(
        None,
        "--format",
        help="Input format (default: yaml; inferred from a source file suffix)",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate and preview without writing"),
) -> None:
    """Validate and optionally apply a vacancy document."""
    vacancy_commands.apply(source=source, document_format=document_format, dry_run=dry_run)


@vacancies_app.command(name="edit", epilog=_FILTERING_HELP)
def vacancies_edit(
    pipeline_id: int | None = typer.Option(
        None,
        "--pipeline-id",
        help="Edit only this pipeline's vacancies (default: all pipelines)",
    ),
) -> None:
    """Dump to a private temporary YAML file, edit, preview, and confirm apply.

    Without ``--pipeline-id`` the document covers all pipelines.
    """
    vacancy_commands.edit(pipeline_id=pipeline_id)


# --- commands ---------------------------------------------------------------
@app.command()
def init(config: Path = typer.Option(..., "--config", "-c", help="Path to pipeline.yaml")) -> None:
    """Parse + validate a pipeline.yaml, validate the cap, and store the pipeline."""
    _run(_init(config))


@_with_services
async def _init(svc: AppServices, config: Path) -> None:
    cfg = _load_config(config)
    # init only stores config + validates the cap; it never runs a pipeline.
    # get-or-keep: a fresh identity is created on first init; a re-init reuses
    # the existing identity (appending a snapshot only when the config differs).
    r = await svc.pipelines.get_or_keep(cfg, source="from_file")
    if r.is_err:
        _fail(r.unwrap_err())
    u = r.unwrap()
    if u.created:
        typer.echo(f"Pipeline {cfg.name!r} created with id {u.pipeline.id}")
    elif u.appended:
        typer.echo(f"Pipeline {cfg.name!r} updated (now snapshot #{u.snapshot.snapshot_no})")
    else:
        typer.echo(f"Pipeline {cfg.name!r} unchanged")


@app.command()
def update(config: Path = typer.Option(..., "--config", "-c", help="Path to pipeline.yaml")) -> None:
    """Update an existing pipeline: append a new config snapshot (id stays stable)."""
    _run(_update(config))


@_with_services
async def _update(svc: AppServices, config: Path) -> None:
    cfg = _load_config(config)
    existing = await svc.pipelines.find_by_name(cfg.name)
    if existing is None:
        _fail(f"No pipeline named {cfg.name!r}; run init first")
    r = await svc.pipelines.new_snapshot(existing.id, cfg, source="from_file")
    if r.is_err:
        _fail(r.unwrap_err())
    u = r.unwrap()
    if u.unchanged:
        typer.echo(f"Pipeline {cfg.name!r} unchanged")
    else:
        typer.echo(f"Pipeline {cfg.name!r} updated (now snapshot #{u.snapshot.snapshot_no})")


_WINDOW_OVERRIDE_HELP = (
    "Run-wide override: when ANY of these flags is given, they replace the "
    "per-search window configured in the pipeline for EVERY search of the pool"
)


def _override_window(
    first_page: int | None,
    page_size: int | None,
    take_pages: int | None,
    from_: int | None,
    to: int | None,
    take: int | None,
) -> SearchWindow | None:
    """The run-wide window override, or ``None`` when no window flag was given.

    ``None`` means "no override": every pool entry uses its own configured
    window. Any given flag switches to override mode, where the missing
    members fall back to the :class:`SearchWindow` defaults.
    """
    given = (first_page, page_size, take_pages, from_, to, take)
    if all(value is None for value in given):
        return None
    defaults = SearchWindow()
    return SearchWindow(
        first_page=first_page if first_page is not None else defaults.first_page,
        page_size=page_size if page_size is not None else defaults.page_size,
        take_pages=take_pages if take_pages is not None else defaults.take_pages,
        from_=from_,
        to=to,
        take=take,
    )


@app.command()
def fetch(
    pipeline_id: int = typer.Option(..., "--pipeline-id", help="Stored pipeline id to fetch for"),
    use_search_config: int | None = typer.Option(
        None,
        "--use-search-config",
        help="Fetch only this search pool entry (0-based); default: every entry in order",
    ),
    first_page: int | None = typer.Option(
        None, "--first-page", help=f"Zero-based first native page of the fetch window. {_WINDOW_OVERRIDE_HELP}"
    ),
    page_size: int | None = typer.Option(
        None, "--page-size", help=f"Vacancies per page: one of 20, 50, 100. {_WINDOW_OVERRIDE_HELP}"
    ),
    take_pages: int | None = typer.Option(
        None, "--take-pages", help=f"Pages in the fetch window. {_WINDOW_OVERRIDE_HELP}"
    ),
    from_: int | None = typer.Option(
        None, "--from", help=f"0-based vacancy index to start the slice at. {_WINDOW_OVERRIDE_HELP}"
    ),
    to: int | None = typer.Option(
        None, "--to", help=f"0-based inclusive end index of the slice. {_WINDOW_OVERRIDE_HELP}"
    ),
    take: int | None = typer.Option(None, "--take", help=f"Vacancy count to persist. {_WINDOW_OVERRIDE_HELP}"),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Overwrite listing fields of already-stored rows and bump fetched_at (insert-only by default)",
    ),
    use_sixel: bool = typer.Option(False, "--use-sixel", help="Use sixel terminal captcha output"),
    use_kitty: bool = typer.Option(False, "--use-kitty", help="Use kitty terminal captcha output"),
    no_captcha_ai: bool = typer.Option(False, "--no-captcha-ai", help="Disable AI captcha solving"),
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="Show run events (-v info lines, -vv client detail)"
    ),
) -> None:
    """Fetch the pipeline's search pool (a window or slice per search entry)."""
    _run(
        _fetch(
            pipeline_id,
            use_search_config,
            _override_window(first_page, page_size, take_pages, from_, to, take),
            refresh,
            use_sixel,
            use_kitty,
            no_captcha_ai,
            verbose,
        )
    )


@_with_services
async def _fetch(
    svc: AppServices,
    pipeline_id: int,
    use_search_config: int | None,
    params: SearchWindow | None,
    refresh: bool,
    use_sixel: bool,
    use_kitty: bool,
    no_captcha_ai: bool,
    verbose: int,
) -> None:
    p = await svc.pipelines.resolve(pipeline_id)
    if p.is_err:
        _fail(p.unwrap_err())
    identity, snapshot = p.unwrap()
    reporter = CliReporter(verbosity=verbose)
    engine_result = build_engine_from_pipeline(
        svc.storage,
        identity,
        snapshot,
        use_sixel=use_sixel,
        use_kitty=use_kitty,
        no_captcha_ai=no_captcha_ai,
        progress=reporter,
    )
    if engine_result.is_err:
        _fail(engine_result.unwrap_err())
    report = await engine_result.unwrap().fetch(
        identity,
        params=params,
        only_search=use_search_config,
        refresh=refresh,
    )
    if report.is_err:
        _fail(report.unwrap_err())
    _print_fetch(report.unwrap())


@app.command()
def search(
    pipeline_id: int = typer.Option(..., "--pipeline-id", help="Stored pipeline id to search for"),
    use_search_config: int | None = typer.Option(
        None,
        "--use-search-config",
        help="Search pool entry to preview (0-based); required when the pipeline has more than one search",
    ),
    query: str | None = typer.Option(None, "--query", help="Override the selected search's query for this preview"),
    params: str | None = typer.Option(
        None,
        "--params",
        help="Filter override: file path, '-' (stdin), or inline {...} JSON/YAML of the board's filter block",
    ),
    document_format: SearchFormat = typer.Option(
        SearchFormat.TEXT,
        "--format",
        case_sensitive=False,
        help="Output format: text (default, brief), json, or yaml. Json/yaml recommended for deep research",
    ),
    first_page: int = typer.Option(0, "--first-page", help="Zero-based first native page of the preview window"),
    page_size: int = typer.Option(100, "--page-size", help="Vacancies per page: one of 20, 50, 100"),
    take_pages: int = typer.Option(1, "--take-pages", help="Pages in the preview window"),
    use_sixel: bool = typer.Option(False, "--use-sixel", help="Use sixel terminal captcha output"),
    use_kitty: bool = typer.Option(False, "--use-kitty", help="Use kitty terminal captcha output"),
    no_captcha_ai: bool = typer.Option(False, "--no-captcha-ai", help="Disable AI captcha solving"),
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="Show run events (-v info lines, -vv client detail)"
    ),
) -> None:
    """Preview one search of a stored pipeline — listing only, nothing stored.

    Runs the pipeline's selected search entry (or the ``--query``/``--params``
    override) at the listing level: one request per page of short results, no
    vacancy bodies, no DB writes. Shows the board-reported total, each
    vacancy's title/company/salary/area plus its DB status (new work vs already
    fetched/scored/applied), and the board's own web search URL for the query.

    The preview window comes from the CLI flags only — the pipeline's
    per-search fetch window config is never used here.

    For deep research yaml/json output format is recommended.
    Text output has significantly less fields and is suitable mostly for quick preview.
    """
    _run(
        _search(
            pipeline_id,
            use_search_config,
            query,
            params,
            document_format,
            SearchWindow(first_page=first_page, page_size=page_size, take_pages=take_pages),
            use_sixel,
            use_kitty,
            no_captcha_ai,
            verbose,
        )
    )


@_with_services
async def _search(  # noqa: PLR0912, PLR0915 - per-entry pool walk + override ladder, kept linear on purpose
    svc: AppServices,
    pipeline_id: int,
    use_search_config: int | None,
    query: str | None,
    params: str | None,
    document_format: SearchFormat,
    fetch_params: SearchWindow,
    use_sixel: bool,
    use_kitty: bool,
    no_captcha_ai: bool,
    verbose: int,
) -> None:
    resolved = await svc.pipelines.resolve(pipeline_id)
    if resolved.is_err:
        _fail(resolved.unwrap_err())
    identity, snapshot = resolved.unwrap()

    # Resolve the search pool first: the preview is single-run and must know
    # which entry's query/filter forms the base before anything is built.
    config_result = build_config_from_pipeline(identity, snapshot)
    if config_result.is_err:
        _fail(config_result.unwrap_err())
    config = config_result.unwrap()
    pool = config.service_section.searches
    if use_search_config is None:
        if len(pool) > 1:
            _fail(search_index_range_error(len(pool)))
        search_index = 0
    else:
        search_index = use_search_config
        if not 0 <= search_index < len(pool):
            _fail(search_index_range_error(len(pool)))

    effective_query = query if query is not None else pool[search_index].query
    query_source = "--query" if query is not None else f"search {search_index}"
    params_source = "pipeline filters (no override)"
    filter_data: _FilterOverride = None
    if params is not None:
        loaded = _load_params_override(params)
        if loaded.is_err:
            _fail(loaded.unwrap_err())
        filter_data, params_source = loaded.unwrap()

    reporter = CliReporter(verbosity=verbose)
    client_result = build_client_from_pipeline(
        identity,
        snapshot,
        use_sixel=use_sixel,
        use_kitty=use_kitty,
        no_captcha_ai=no_captcha_ai,
        search_index=search_index,
        query_override=query,
        filter_override=filter_data,
        reporter=reporter,
    )
    if client_result.is_err:
        _fail(client_result.unwrap_err())
    client = client_result.unwrap()

    # Reuse the fetch plan's validation matrix (page sizes, window vs the
    # board's search cap); without slice flags the slice is the whole window.
    plan_result = plan_fetch(
        fetch_params,
        max_search_items=client.service_info.max_search_items,
        origin=f"search {search_index}: ",
    )
    if plan_result.is_err:
        await client.aclose()
        _fail(plan_result.unwrap_err())
    plan = plan_result.unwrap()

    try:
        listing_result = await client.list_vacancies(
            search_index,
            offset=plan.slice_start,
            limit=plan.slice_end - plan.slice_start,
            page_size=plan.page_size,
        )
    finally:
        await client.aclose()
    if listing_result.is_err:
        _fail(f"search failed: {listing_result.unwrap_err().message}")
    listing = listing_result.unwrap()

    # Read-only DB join: one all-states read maps each listed external id onto
    # its stored row (or absence = "new"). No upserts, no audit entries.
    stored = await svc.storage.vacancies.list_states_by_pipeline(identity.id)
    states_by_external_id: dict[str, VacancyRecord] = {record.external_id: record for record in stored}
    preview = build_preview(
        listing,
        PreviewContext(
            pipeline=identity,
            query=effective_query,
            query_source=query_source,
            params_source=params_source,
            board_cap=client.service_info.max_search_items,
        ),
        states_by_external_id,
    )
    match document_format:
        case SearchFormat.TEXT:
            typer.echo(render_text_preview(preview))
        case SearchFormat.JSON:
            typer.echo(json.dumps(preview_document(preview), indent=2, ensure_ascii=False))
        case SearchFormat.YAML:
            typer.echo(yaml.safe_dump(preview_document(preview), allow_unicode=True, sort_keys=False).rstrip())


@app.command()
def score(
    pipeline_id: int = typer.Option(..., "--pipeline-id", help="Stored pipeline id to score"),
    from_: int | None = typer.Option(None, "--from", help="0-based offset into the id-ordered vacancy list"),
    to: int | None = typer.Option(None, "--to", help="0-based inclusive end offset into the vacancy list"),
    take: int | None = typer.Option(None, "--take", help="Count of vacancies starting at --from"),
    skip_already_processed: bool = typer.Option(False, "--skip-already-processed"),
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="Show run items (-v info lines, -vv client detail)"
    ),
) -> None:
    """Score the fetched vacancies of a stored pipeline."""
    _run(_score(pipeline_id, from_, to, take, skip_already_processed, verbose))


@_with_services
async def _score(
    svc: AppServices,
    pipeline_id: int,
    from_: int | None,
    to: int | None,
    take: int | None,
    skip_already_processed: bool,
    verbose: int,
) -> None:
    p = await svc.pipelines.resolve(pipeline_id)
    if p.is_err:
        _fail(p.unwrap_err())
    identity, snapshot = p.unwrap()
    reporter = CliReporter(verbosity=verbose)
    engine_result = build_engine_from_pipeline(
        svc.storage, identity, snapshot, require_captcha=False, progress=reporter
    )
    if engine_result.is_err:
        _fail(engine_result.unwrap_err())
    selector = _make_selector(from_, to, take, skip_already_processed)
    report = await engine_result.unwrap().score(identity, selector=selector)
    if report.is_err:
        _fail(report.unwrap_err())
    _print_score(report.unwrap())


@app.command()
def generate(
    pipeline_id: int = typer.Option(..., "--pipeline-id", help="Stored pipeline id to generate for"),
    from_: int | None = typer.Option(None, "--from", help="0-based offset into the id-ordered vacancy list"),
    to: int | None = typer.Option(None, "--to", help="0-based inclusive end offset into the vacancy list"),
    take: int | None = typer.Option(None, "--take", help="Count of vacancies starting at --from"),
    skip_already_processed: bool = typer.Option(False, "--skip-already-processed"),
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="Show run items (-v info lines, -vv client detail)"
    ),
) -> None:
    """Generate cover letters for the fetched vacancies of a stored pipeline."""
    _run(_generate(pipeline_id, from_, to, take, skip_already_processed, verbose))


@_with_services
async def _generate(
    svc: AppServices,
    pipeline_id: int,
    from_: int | None,
    to: int | None,
    take: int | None,
    skip_already_processed: bool,
    verbose: int,
) -> None:
    p = await svc.pipelines.resolve(pipeline_id)
    if p.is_err:
        _fail(p.unwrap_err())
    identity, snapshot = p.unwrap()
    reporter = CliReporter(verbosity=verbose)
    engine_result = build_engine_from_pipeline(
        svc.storage, identity, snapshot, require_captcha=False, progress=reporter
    )
    if engine_result.is_err:
        _fail(engine_result.unwrap_err())
    selector = _make_selector(from_, to, take, skip_already_processed)
    report = await engine_result.unwrap().generate_cv(identity, selector=selector)
    if report.is_err:
        _fail(report.unwrap_err())
    _print_generate(report.unwrap())


@app.command()
def apply(
    pipeline_id: int = typer.Option(..., "--pipeline-id", help="Stored pipeline id to apply for"),
    from_: int | None = typer.Option(None, "--from", help="0-based offset into the id-ordered vacancy list"),
    to: int | None = typer.Option(None, "--to", help="0-based inclusive end offset into the vacancy list"),
    take: int | None = typer.Option(None, "--take", help="Count of vacancies starting at --from"),
    skip_already_processed: bool = typer.Option(False, "--skip-already-processed"),
    min_score: int | None = typer.Option(
        None,
        "--min-score",
        min=1,
        max=5,
        help="Apply to vacancies scored at or above N for this run (default: the pipeline config threshold)",
    ),
    include_unscored: bool = typer.Option(
        False, "--include-unscored", help="Also apply to vacancies that have no score yet"
    ),
    allow_without_letter: bool = typer.Option(
        False,
        "--allow-without-letter",
        help="Also apply to vacancies with no generated cover letter (no message is sent)",
    ),
    vacancy_ids: list[str] = typer.Option(
        [],
        "--vacancy-id",
        help=(
            "Apply ONLY to these board vacancy ids (repeatable), bypassing the score/letter/manual-skip"
            " filters; prior decisions still block unless --force"
        ),
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="With --vacancy-id only: also re-attempt vacancies that already have a decision (applied/skipped/error)",
    ),
    has_hh_tests: str = typer.Option(
        "any",
        "--has-hh-tests",
        help="Narrow the selection by the fetch-time hh test flag: any | only_with_hh_tests | only_without_hh_tests",
    ),
    test_answers: Path | None = typer.Option(
        None,
        "--test-answers",
        help="Answers file for hh screening tests (async/solved flow); wins over AI solving",
    ),
    no_test_ai: bool = typer.Option(False, "--no-test-ai", help="Disable AI hh test solving"),
    use_sixel: bool = typer.Option(False, "--use-sixel", help="Use sixel terminal captcha output"),
    use_kitty: bool = typer.Option(False, "--use-kitty", help="Use kitty terminal captcha output"),
    no_captcha_ai: bool = typer.Option(False, "--no-captcha-ai", help="Disable AI captcha solving"),
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="Show run items (-v info lines, -vv client detail)"
    ),
) -> None:
    """Apply to the scored vacancies of a stored pipeline (filters relaxable per run)."""
    _run(
        _apply(
            pipeline_id,
            from_,
            to,
            take,
            skip_already_processed,
            min_score,
            include_unscored,
            allow_without_letter,
            vacancy_ids,
            force,
            has_hh_tests,
            test_answers,
            no_test_ai,
            use_sixel,
            use_kitty,
            no_captcha_ai,
            verbose,
        )
    )


def _validate_apply_flags(
    vacancy_ids: list[str],
    force: bool,
    from_: int | None,
    to: int | None,
    take: int | None,
    skip_already_processed: bool,
) -> None:
    """Fail fast on contradictory apply flags (presentation-level validation)."""
    if force and not vacancy_ids:
        _fail("--force requires --vacancy-id")
    if vacancy_ids and (from_ is not None or to is not None or take is not None or skip_already_processed):
        _fail("Cannot combine --vacancy-id with --from/--to/--take/--skip-already-processed")


def _parse_has_hh_tests(value: str) -> Literal["any", "only_with_hh_tests", "only_without_hh_tests"]:
    """Narrow the CLI ``--has-hh-tests`` extension on its closed value set."""
    if value not in ("any", "only_with_hh_tests", "only_without_hh_tests"):
        _fail("--has-hh-tests must be one of: any | only_with_hh_tests | only_without_hh_tests")
    return value  # type: ignore[return-value]  # rationale: guarded by the membership check above


@hh_tests_app.command(name="dump")
def hh_tests_dump(
    pipeline_id: int = typer.Option(..., "--pipeline-id", help="Stored pipeline id to dump tests for"),
    from_: int | None = typer.Option(None, "--from", help="0-based offset into the id-ordered vacancy list"),
    to: int | None = typer.Option(None, "--to", help="0-based inclusive end offset into the vacancy list"),
    take: int | None = typer.Option(None, "--take", help="Count of vacancies starting at --from"),
    skip_already_processed: bool = typer.Option(False, "--skip-already-processed"),
    min_score: int | None = typer.Option(
        None,
        "--min-score",
        min=1,
        max=5,
        help="Dump tests of vacancies scored at or above N (default: the pipeline config threshold)",
    ),
    include_unscored: bool = typer.Option(
        False, "--include-unscored", help="Also consider vacancies that have no score yet"
    ),
    allow_without_letter: bool = typer.Option(
        False,
        "--allow-without-letter",
        help="Also consider vacancies with no generated cover letter",
    ),
    has_hh_tests: str = typer.Option(
        "only_with_hh_tests",
        "--has-hh-tests",
        help="Selection by the fetch-time hh test flag: any | only_with_hh_tests | only_without_hh_tests",
    ),
    output: Path = typer.Option(
        Path("test_problems.json"), "--output", help="Write the problems document to this file"
    ),
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="Show run items (-v info lines, -vv client detail)"
    ),
) -> None:
    """Fetch screening tests of eligible vacancies into a problems JSON file.

    The file is then solved by a human, an offline AI pass, or the answers-file
    flow, and ``apply --test-answers FILE`` consumes it. No application is made.
    """
    output = output.expanduser()
    _run(
        _hh_tests_dump(
            pipeline_id,
            from_,
            to,
            take,
            skip_already_processed,
            min_score,
            include_unscored,
            allow_without_letter,
            has_hh_tests,
            output,
            verbose,
        )
    )


@_with_services
async def _hh_tests_dump(
    svc: AppServices,
    pipeline_id: int,
    from_: int | None,
    to: int | None,
    take: int | None,
    skip_already_processed: bool,
    min_score: int | None,
    include_unscored: bool,
    allow_without_letter: bool,
    has_hh_tests: str,
    output: Path,
    verbose: int,
) -> None:
    has_hh_tests_value = _parse_has_hh_tests(has_hh_tests)
    p = await svc.pipelines.resolve(pipeline_id)
    if p.is_err:
        _fail(p.unwrap_err())
    identity, snapshot = p.unwrap()

    filters = ApplyFilters(
        min_score=min_score,
        include_unscored=include_unscored,
        allow_without_letter=allow_without_letter,
        has_hh_test=has_hh_tests_value,
    )
    reporter = CliReporter(verbosity=verbose)
    engine_result = build_engine_from_pipeline(svc.storage, identity, snapshot, progress=reporter)
    if engine_result.is_err:
        _fail(engine_result.unwrap_err())
    records_result = await engine_result.unwrap().dump_hh_tests(
        identity,
        selector=_make_selector(from_, to, take, skip_already_processed),
        filters=filters,
    )
    if records_result.is_err:
        _fail(records_result.unwrap_err())
    records = records_result.unwrap()
    # Async boundary: small user-facing document; a sync write is fine here.
    output.write_text(dump_problems_to_json(records), encoding="utf-8")  # noqa: ASYNC240 - tiny CLI-owned file write
    typer.echo(f"hh-tests dump: {len(records)} test(s) written to {output}")


@_with_services
async def _apply(
    svc: AppServices,
    pipeline_id: int,
    from_: int | None,
    to: int | None,
    take: int | None,
    skip_already_processed: bool,
    min_score: int | None,
    include_unscored: bool,
    allow_without_letter: bool,
    vacancy_ids: list[str],
    force: bool,
    has_hh_tests: str,
    test_answers: Path | None,
    no_test_ai: bool,
    use_sixel: bool,
    use_kitty: bool,
    no_captcha_ai: bool,
    verbose: int,
) -> None:
    _validate_apply_flags(vacancy_ids, force, from_, to, take, skip_already_processed)
    has_hh_tests_value = _parse_has_hh_tests(has_hh_tests)
    p = await svc.pipelines.resolve(pipeline_id)
    if p.is_err:
        _fail(p.unwrap_err())
    identity, snapshot = p.unwrap()

    filters = ApplyFilters(
        min_score=min_score,
        include_unscored=include_unscored,
        allow_without_letter=allow_without_letter,
        only_external_ids=frozenset(vacancy_ids),
        force_decided=force,
        has_hh_test=has_hh_tests_value,
    )
    # Run header: declare the run's relaxation posture up front so later
    # board outcomes (e.g. "Letter required") can't read as ignored flags.
    if filters.only_external_ids:
        forced = f"apply: forced {len(filters.only_external_ids)} id(s) — eligibility filters bypassed"
        if filters.force_decided:
            forced += ", decided re-attempted"
        typer.echo(forced)
    else:
        relaxations = [
            *(["unscored allowed"] if filters.include_unscored else []),
            *(["letterless allowed"] if filters.allow_without_letter else []),
        ]
        if relaxations:
            typer.echo(f"apply: run relaxed — {', '.join(relaxations)}")
    reporter = CliReporter(verbosity=verbose)
    engine_result = build_engine_from_pipeline(
        svc.storage,
        identity,
        snapshot,
        use_sixel=use_sixel,
        use_kitty=use_kitty,
        no_captcha_ai=no_captcha_ai,
        progress=reporter,
    )
    if engine_result.is_err:
        _fail(engine_result.unwrap_err())
    selector = None if filters.only_external_ids else _make_selector(from_, to, take, skip_already_processed)
    report = await engine_result.unwrap().apply(
        identity,
        selector=selector,
        filters=filters,
        test_answers=test_answers,
        no_test_ai=no_test_ai,
    )
    if report.is_err:
        _fail(report.unwrap_err())
    _print_apply(report.unwrap())


@app.command()
def run(
    pipeline_id: int | None = typer.Option(None, "--pipeline-id", help="Stored pipeline id to run"),
    new_from_config: Path | None = typer.Option(
        None, "--new-from-config", help="Create a new pipeline from a pipeline.yaml and run it"
    ),
    from_: int | None = typer.Option(None, "--from"),
    to: int | None = typer.Option(None, "--to"),
    take: int | None = typer.Option(None, "--take"),
    first_page: int | None = typer.Option(
        None, "--first-page", help=f"Zero-based first native page of the run's fetch window. {_WINDOW_OVERRIDE_HELP}"
    ),
    page_size: int | None = typer.Option(
        None, "--page-size", help=f"Vacancies per page: one of 20, 50, 100. {_WINDOW_OVERRIDE_HELP}"
    ),
    take_pages: int | None = typer.Option(
        None, "--take-pages", help=f"Pages in the run's fetch window. {_WINDOW_OVERRIDE_HELP}"
    ),
    skip_already_processed: bool = typer.Option(False, "--skip-already-processed"),
    use_sixel: bool = typer.Option(False, "--use-sixel", help="Use sixel terminal captcha output"),
    use_kitty: bool = typer.Option(False, "--use-kitty", help="Use kitty terminal captcha output"),
    no_captcha_ai: bool = typer.Option(False, "--no-captcha-ai", help="Disable AI captcha solving"),
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="Show run items (-v info lines, -vv client detail)"
    ),
) -> None:
    """Run fetch → score → generate_cv → apply for a stored (or new-from-config) pipeline."""
    _run(
        _run_pipeline(
            pipeline_id,
            new_from_config,
            from_,
            to,
            take,
            first_page,
            page_size,
            take_pages,
            skip_already_processed,
            use_sixel,
            use_kitty,
            no_captcha_ai,
            verbose,
        )
    )


@_with_services
async def _run_pipeline(
    svc: AppServices,
    pipeline_id: int | None,
    new_from_config: Path | None,
    from_: int | None,
    to: int | None,
    take: int | None,
    first_page: int | None,
    page_size: int | None,
    take_pages: int | None,
    skip_already_processed: bool,
    use_sixel: bool,
    use_kitty: bool,
    no_captcha_ai: bool,
    verbose: int,
) -> None:
    if pipeline_id is not None and new_from_config is not None:
        _fail("Cannot use both --pipeline-id and --new-from-config.")
    if pipeline_id is None and new_from_config is None:
        _fail("Provide one of --pipeline-id or --new-from-config.")

    selector = _make_selector(from_, to, take, skip_already_processed)

    if new_from_config is not None:
        # Load + validate a fresh yaml, then get-or-keep: resolve the identity
        # by name — first run creates it, a re-run with the same config is a
        # no-op (append semantics; never soft-deletes, never re-ids) — and run
        # the full composition against the returned identity + its snapshot
        # (same as --pipeline-id does).
        cfg = _load_config(new_from_config)
        r = await svc.pipelines.get_or_keep(cfg, source="from_file")
        if r.is_err:
            _fail(r.unwrap_err())
        m = r.unwrap()
        identity = m.pipeline
        snapshot = m.snapshot
    else:
        assert pipeline_id is not None  # guaranteed by the exclusivity checks above
        p = await svc.pipelines.resolve(pipeline_id)
        if p.is_err:
            _fail(p.unwrap_err())
        identity, snapshot = p.unwrap()

    reporter = CliReporter(verbosity=verbose)
    engine_result = build_engine_from_pipeline(
        svc.storage,
        identity,
        snapshot,
        use_sixel=use_sixel,
        use_kitty=use_kitty,
        no_captcha_ai=no_captcha_ai,
        progress=reporter,
    )
    if engine_result.is_err:
        _fail(engine_result.unwrap_err())
    report = await engine_result.unwrap().run(
        identity,
        selector=selector,
        fetch_params=_override_window(first_page, page_size, take_pages, None, None, None),
    )
    if report.is_err:
        _fail(report.unwrap_err())
    full = report.unwrap()
    _print_fetch(full.fetch)
    _print_score(full.score)
    _print_generate(full.generate_cv)
    _print_apply(full.apply)


@app.command()
def status(pipeline_id: int | None = typer.Option(None, "--pipeline-id")) -> None:
    """Show stored pipelines and their next-action buckets."""
    _run(_status(pipeline_id))


@_with_services
async def _status(svc: AppServices, pipeline_id: int | None) -> None:  # noqa: PLR0912 - per-bucket status table
    pipelines = await svc.pipelines.list()
    if not pipelines:
        typer.echo("No pipelines stored.")
        return
    for identity in pipelines:
        if pipeline_id is not None and identity.id != pipeline_id:
            continue
        resolved = await svc.pipelines.resolve(identity.id)
        if resolved.is_err:
            _fail(resolved.unwrap_err())
        _identity, snapshot = resolved.unwrap()
        vacancies = await svc.storage.vacancies.list_by_pipeline(identity.id)

        # One decisive bucket per vacancy (most decisive state wins), derived
        # from stored fields + the current snapshot's threshold — no stored
        # status column. Buckets are action-oriented: what the user would run
        # next (need-scoring → need-generate → ready-to-apply), then the
        # informational ones (below-threshold / paused / stale), then the
        # apply ledger (applied / declined / failed), each printed only when
        # nonzero. Scoring/lettering error rows fold into their re-run bucket
        # (re-running the stage retries them).
        ready = need_generate = need_scoring = below = paused = stale = 0
        applied = declined = failed = 0
        threshold = snapshot.min_required_score
        for vacancy in vacancies:
            stale += int(
                _is_stale(vacancy.fetched_at, vacancy.scored_at) or _is_stale(vacancy.fetched_at, vacancy.generated_at)
            )
            if vacancy.manual_skip:
                paused += 1
                continue
            match vacancy.apply_status:
                case "applied":
                    applied += 1
                case "skipped":
                    declined += 1
                case "error":
                    failed += 1
                case None | "pending":
                    if vacancy.cover_letter:
                        ready += 1
                    elif vacancy.cover_letter_error is not None:
                        need_generate += 1
                    elif vacancy.score is None or vacancy.score_error is not None:
                        need_scoring += 1
                    elif vacancy.score >= threshold:
                        need_generate += 1
                    else:
                        below += 1

        buckets = [
            f"{count} {text}"
            for count, text in (
                (need_scoring, "need-scoring"),
                (need_generate, "need-generate"),
                (ready, "ready-to-apply"),
                (below, "below-threshold"),
                (paused, "paused"),
                (stale, "stale"),
            )
            if count
        ]
        ledger = [
            f"{count} {text}"
            for count, text in (
                (applied, "applied"),
                (declined, "declined"),
                (failed, "failed"),
            )
            if count
        ]
        line = f"Pipeline {identity.id} {identity.name!r} service={snapshot.service} vacancies={len(vacancies)}"
        if buckets or ledger:
            line += f" — {' · '.join(buckets + ledger)}"
        typer.echo(line)


@app.command()
def resumes(
    pipeline_id: int = typer.Option(..., "--pipeline-id", help="Stored pipeline id to list resumes for"),
    use_sixel: bool = typer.Option(False, "--use-sixel", help="Use sixel terminal captcha output"),
    use_kitty: bool = typer.Option(False, "--use-kitty", help="Use kitty terminal captcha output"),
    no_captcha_ai: bool = typer.Option(False, "--no-captcha-ai", help="Disable AI captcha solving"),
    verbose: int = typer.Option(
        0, "--verbose", "-v", count=True, help="Show run events (-v info lines, -vv client detail)"
    ),
) -> None:
    """List the account's owned resumes (id, updated, title)."""
    _run(_resumes(pipeline_id, use_sixel, use_kitty, no_captcha_ai, verbose))


@_with_services
async def _resumes(
    svc: AppServices,
    pipeline_id: int,
    use_sixel: bool,
    use_kitty: bool,
    no_captcha_ai: bool,
    verbose: int,
) -> None:
    resolved = await svc.pipelines.resolve(pipeline_id)
    if resolved.is_err:
        _fail(resolved.unwrap_err())
    identity, snapshot = resolved.unwrap()
    client_result = build_client_from_pipeline(
        identity,
        snapshot,
        use_sixel=use_sixel,
        use_kitty=use_kitty,
        no_captcha_ai=no_captcha_ai,
        reporter=CliReporter(verbosity=verbose),
    )
    if client_result.is_err:
        _fail(client_result.unwrap_err())
    client = client_result.unwrap()
    try:
        listed = await client.get_resumes()
    finally:
        await client.aclose()
    if listed.is_err:
        _fail(listed.unwrap_err().message)
    resumes = listed.unwrap()
    if not resumes:
        typer.echo("No resumes on this account.")
        return
    for resume in resumes:
        updated = resume.updated_at if resume.updated_at is not None else "-"
        typer.echo(f"{resume.resume_id}  {updated}  {resume.title}")


def _is_stale(fetched_at: str | None, artifact_at: str | None) -> bool:
    """NULL-safe staleness check on the shared timestamp format.

    Same-format ``YYYY-MM-DD HH:MM:SS`` UTC strings compare lexicographically;
    a missing artifact timestamp (never written) is NOT stale — it simply has
    no artifact yet.
    """
    return fetched_at is not None and artifact_at is not None and fetched_at > artifact_at


@app.command(name="pipelines-list")
def pipelines_list(
    snapshots: bool = typer.Option(False, "--snapshots", help="Show each pipeline's snapshot history"),
) -> None:
    """List stored pipelines (one line per identity: id, name, service).

    With ``--snapshots``, each identity line is followed by its nested snapshot
    history (``#<no>\\t<source>\\t<created_at>``).
    """
    _run(_pipelines_list(snapshots))


@_with_services
async def _pipelines_list(svc: AppServices, snapshots: bool) -> None:
    pipelines = await svc.pipelines.list()
    if not pipelines:
        typer.echo("No pipelines stored.")
        return
    for identity in pipelines:
        resolved = await svc.pipelines.resolve(identity.id)
        if resolved.is_err:
            _fail(resolved.unwrap_err())
        _identity, snapshot = resolved.unwrap()
        typer.echo(f"{identity.id}\t{identity.name}\t{snapshot.service}")
        if snapshots:
            history = await svc.pipelines.snapshots(identity.id)
            if history.is_err:
                _fail(history.unwrap_err())
            for snap in history.unwrap():
                typer.echo(f"  #{snap.snapshot_no}\t{snap.source}\t{snap.created_at}")


def main() -> None:
    """Entry point for the ``jobfucker`` console script."""
    app()
