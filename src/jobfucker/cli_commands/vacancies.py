"""Terminal workflow client for vacancy review documents.

The Typer router owns the public command signature. This module owns terminal
I/O, service lifecycle, document file handling, editor invocation, and command
result formatting. Vacancy planning and persistence remain in the application
service so other interfaces can reuse them without depending on Typer.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Concatenate, NoReturn

import typer
from rusty_results.prelude import Err, Ok, Result
from typer import Abort

from jobfucker.app.services import AppServices
from jobfucker.app.vacancy_documents import VacancyPlanSummary
from jobfucker.table_documents.codec import DocumentFormat, OverwriteMode, write_document

FILTERING_HELP = """Filtering and batching

Filter entries under `.vacancies`; edits to the read-only
`context_edit_not_allowed` block are ignored. Removing a vacancy from the dump
just selects a smaller batch for user. Missing vacancies do not delete them
from the database.

`dump` and `edit` accept an optional `--pipeline-id N` to scope the document to
one pipeline; without it all pipelines are included. `apply` is document-driven
and has no pipeline flag.

Below you can see pipelines examples.

YAML with the Mike Farah/Go yq implementation:

```
jobfucker vacancies dump | yq '
.vacancies |= with_entries(
    select(.value.editable.score != null and .value.editable.score >= 4)
)
' | $EDITOR | jobfucker vacancies apply
```

JSON with jq:

```
jobfucker vacancies dump --format json | jq '
.vacancies |= with_entries(
    select(.value.editable.score != null and .value.editable.score >= 4)
)
' | $EDITOR | jobfucker vacancies apply --format json
```
"""


def _fail(message: str) -> NoReturn:
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


async def _with_services[**P](
    operation: Callable[Concatenate[AppServices, P], Awaitable[None]],
    pipeline_id: int | None = None,
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> None:
    result = await AppServices.open(vacancies_pipeline_id=pipeline_id)
    if result.is_err:
        _fail(result.unwrap_err())
    services = result.unwrap()
    try:
        await operation(services, *args, **kwargs)
    finally:
        await services.close()


def _document_format(path: Path) -> DocumentFormat:
    suffix = path.suffix.lower()
    if suffix in (".yaml", ".yml"):
        return DocumentFormat.YAML
    if suffix == ".json":
        return DocumentFormat.JSON
    _fail(f"Unsupported vacancy document suffix {path.suffix!r}; use .yaml, .yml, or .json")


def _resolve_document_format(path: Path | None, requested: DocumentFormat | None) -> DocumentFormat:
    if path is None:
        return requested or DocumentFormat.YAML
    inferred = _document_format(path)
    if requested is not None and requested is not inferred:
        _fail(f"--format {requested.value} conflicts with document suffix {path.suffix!r}")
    return requested or inferred


def _read_document_result(path: Path) -> Result[bytes, str]:
    try:
        return Ok(path.read_bytes())
    except OSError as exc:
        return Err(f"Failed to read {path}: {exc}")


def _read_input(source: Path | None) -> Result[bytes, str]:
    if source is not None:
        return _read_document_result(source)
    try:
        return Ok(typer.get_binary_stream("stdin").read())
    except OSError as exc:
        return Err(f"Failed to read vacancy document from stdin: {exc}")


def _print_vacancy_summary(summary: VacancyPlanSummary) -> None:
    typer.echo(
        "vacancies: "
        f"considered={summary.rows_considered} changed={summary.changed} unchanged={summary.unchanged} "
        f"soft_deleted={summary.soft_deleted} restored={summary.restored} "
        f"ignored_read_only={summary.ignored_read_only}"
    )


async def _run_editor(editor: str, document_path: Path) -> Result[None, str]:
    try:
        arguments = shlex.split(editor)
    except ValueError as exc:
        return Err(f"Invalid EDITOR command: {exc}")
    if not arguments:
        return Err("EDITOR is empty; set it to an editor command")
    try:
        process = await asyncio.create_subprocess_exec(*arguments, str(document_path))
        return_code = await process.wait()
    except OSError as exc:
        return Err(f"Failed to launch EDITOR: {exc}")
    if return_code != 0:
        return Err(f"EDITOR exited with status {return_code}")
    return Ok(None)


def _create_editor_document(content: bytes) -> Result[Path, str]:
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".yaml",
            prefix="jobfucker-vacancies-",
            delete=False,
        ) as file:
            temporary_path = Path(file.name)
            file.write(content)
        temporary_path.chmod(0o600)
        return Ok(temporary_path)
    except OSError as exc:
        return Err(f"Failed to create temporary vacancy document: {exc}")


def _remove_document(path: Path) -> None:
    path.unlink(missing_ok=True)


def dump(
    *,
    output: Path | None,
    document_format: DocumentFormat | None,
    force: bool,
    pipeline_id: int | None = None,
) -> None:
    """Run the vacancy dump terminal workflow."""
    if force and output is None:
        _fail("--force requires --output")
    resolved_format = _resolve_document_format(output, document_format)
    asyncio.run(_with_services(_dump, pipeline_id, output, resolved_format, force=force))


async def _dump(
    services: AppServices,
    output: Path | None,
    document_format: DocumentFormat,
    *,
    force: bool,
) -> None:
    dump_result = await services.vacancies.dump_encoded(document_format)
    if dump_result.is_err:
        _fail(dump_result.unwrap_err())
    vacancy_dump = dump_result.unwrap()
    if output is None:
        typer.echo(vacancy_dump.content.decode("utf-8"), nl=False)
        return
    overwrite_mode = OverwriteMode.REPLACE if force else OverwriteMode.REFUSE
    write_result = write_document(output, vacancy_dump.content, overwrite_mode)
    if write_result.is_err:
        _fail(write_result.unwrap_err())
    typer.echo(f"Dumped vacancies to {output} rows={vacancy_dump.row_count}")


def apply(*, source: Path | None, document_format: DocumentFormat | None, dry_run: bool) -> None:
    """Run the vacancy document apply terminal workflow."""
    resolved_format = _resolve_document_format(source, document_format)
    input_result = _read_input(source)
    if input_result.is_err:
        _fail(input_result.unwrap_err())
    asyncio.run(_with_services(_apply, None, input_result.unwrap(), resolved_format, dry_run=dry_run))


async def _apply(
    services: AppServices,
    content: bytes,
    document_format: DocumentFormat,
    *,
    dry_run: bool,
) -> None:
    plan_result = await services.vacancies.plan_encoded(content, document_format)
    if plan_result.is_err:
        _fail(plan_result.unwrap_err())
    plan = plan_result.unwrap()
    _print_vacancy_summary(plan.summary)
    if dry_run:
        return
    apply_result = await services.vacancies.apply(plan)
    if apply_result.is_err:
        _fail(apply_result.unwrap_err())


def edit(*, pipeline_id: int | None = None) -> None:
    """Run the interactive vacancy editor terminal workflow."""
    asyncio.run(_with_services(_edit, pipeline_id))


async def _edit(services: AppServices) -> None:
    editor = os.environ.get("EDITOR", "").strip()
    if not editor:
        _fail("EDITOR is not set; set it to the editor command to use")

    dump_result = await services.vacancies.dump_encoded(DocumentFormat.YAML)
    if dump_result.is_err:
        _fail(dump_result.unwrap_err())
    temporary_result = _create_editor_document(dump_result.unwrap().content)
    if temporary_result.is_err:
        _fail(temporary_result.unwrap_err())
    temporary_path = temporary_result.unwrap()

    editor_result = await _run_editor(editor, temporary_path)
    if editor_result.is_err:
        _remove_document(temporary_path)
        _fail(editor_result.unwrap_err())

    edited_result = _read_document_result(temporary_path)
    if edited_result.is_err:
        _fail(f"{edited_result.unwrap_err()}. Edited document preserved at {temporary_path}")
    edited_content = edited_result.unwrap()
    if not edited_content:
        _fail(f"Edited document is empty. Edited document preserved at {temporary_path}")

    plan_result = await services.vacancies.plan_encoded(edited_content, DocumentFormat.YAML)
    if plan_result.is_err:
        _fail(f"{plan_result.unwrap_err()}. Edited document preserved at {temporary_path}")
    plan = plan_result.unwrap()
    _print_vacancy_summary(plan.summary)
    if plan.summary.changed == 0:
        _remove_document(temporary_path)
        typer.echo("No changes to apply.")
        return

    try:
        confirmed = await asyncio.to_thread(lambda: typer.confirm("Apply changes?", default=False))
    except Abort:
        confirmed = False
    if not confirmed:
        _remove_document(temporary_path)
        typer.echo("Changes were not applied.")
        return

    apply_result = await services.vacancies.apply(plan)
    if apply_result.is_err:
        _fail(f"{apply_result.unwrap_err()}. Edited document preserved at {temporary_path}")
    _remove_document(temporary_path)
    typer.echo("Vacancy changes applied.")
