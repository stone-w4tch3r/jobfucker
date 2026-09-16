"""CLI integration tests for ``jobfucker vacancies``.

Scenario: dump, preview, and apply use one document contract
  Given one stored vacancy
  When it is dumped, edited, previewed, and applied
  Then preview writes nothing and apply persists the same planned edit

Scenario: editor validation failure is recoverable
  Given a fake editor that writes an invalid score
  When ``vacancies edit`` validates the edited file
  Then the database is unchanged and the mode-0600 recovery path is printed

Scenario: an unchanged edit needs no confirmation
  Given an editor that leaves the vacancy document unchanged
  When ``vacancies edit`` returns from the editor
  Then it reports no changes and does not ask whether to apply

Scenario: dump defaults to a YAML stream
  Given stored vacancies and no output path
  When ``vacancies dump`` runs
  Then stdout contains only a parseable YAML document

Scenario: dump format and overwrite are explicit
  Given a requested JSON stream or an existing output file
  When ``--format json`` or ``-f`` is supplied
  Then the requested representation is emitted and replacement is opt-in

Scenario: apply consumes a document stream
  Given a vacancy document transformed in a pipeline
  When ``vacancies apply`` has no source file
  Then it reads YAML from stdin by default and applies the edit

Scenario: streamed JSON format is explicit
  Given a JSON vacancy document on stdin
  When ``vacancies apply --format json`` runs
  Then the JSON stream is decoded and applied

Scenario: dump and edit accept an optional pipeline scope
  Given vacancies in two pipelines
  When ``--pipeline-id`` selects one pipeline or an unknown id
  Then the document holds only scoped rows and unknown ids fail with the standard message

Scenario: EDITOR resolution is OS-aware
  Given EDITOR values holding a Windows spaced path, a bare command, or nothing at all
  When the editor argv is resolved
  Then Windows keeps quoted paths as one token (defaulting to notepad) and POSIX splits args or fails explicitly
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter
from rusty_results.prelude import Ok, Result
from typer.testing import CliRunner

from jobfucker.app.services import AppServices
from jobfucker.cli import app
from jobfucker.cli_commands.vacancies import _resolve_editor_argv  # pyright: ignore[reportPrivateUsage]
from jobfucker.storage.db import Storage
from jobfucker.testing.step_runner import async_run
from test.storage.builders import make_pipeline, make_vacancy

runner = CliRunner()


def _install_services(monkeypatch: pytest.MonkeyPatch, storage: Storage) -> None:
    real_open = AppServices.open

    async def fake_open(
        *,
        storage_override: Storage | None = None,
        vacancies_pipeline_id: int | None = None,
    ) -> Result[AppServices, str]:
        del storage_override
        return await real_open(storage_override=storage, vacancies_pipeline_id=vacancies_pipeline_id)

    opener: Callable[..., Awaitable[Result[AppServices, str]]] = fake_open
    monkeypatch.setattr("jobfucker.cli.AppServices.open", opener)


def _fake_which(resolved: str | None) -> Callable[[str], str | None]:
    def which(_command: str) -> str | None:
        return resolved

    return which


def test_editor_resolution_keeps_windows_spaced_path_as_one_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    editor = '"C:\\Program Files\\Foo\\bar.exe"'

    result = _resolve_editor_argv(editor)

    assert result.is_ok and result.unwrap() == ["C:\\Program Files\\Foo\\bar.exe"]


def test_editor_resolution_resolves_windows_bare_command_via_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(shutil, "which", _fake_which("C:\\Users\\dev\\AppData\\code.cmd"))

    result = _resolve_editor_argv("code")

    assert result.is_ok and result.unwrap() == ["C:\\Users\\dev\\AppData\\code.cmd"]


def test_editor_resolution_defaults_to_notepad_on_windows_without_editor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(os, "name", "nt")

    result = _resolve_editor_argv(None)

    assert result.is_ok and result.unwrap() == ["notepad"]


def test_editor_resolution_splits_posix_editor_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(shutil, "which", _fake_which("/usr/bin/code"))

    result = _resolve_editor_argv("code -w")

    assert result.is_ok and result.unwrap() == ["/usr/bin/code", "-w"]


def test_editor_resolution_requires_editor_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")

    result = _resolve_editor_argv(None)

    assert result.is_err and result.unwrap_err() == "EDITOR is not set; set it to the editor command to use"


def test_editor_resolution_fails_when_posix_command_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(shutil, "which", _fake_which(None))

    result = _resolve_editor_argv("missing-editor-binary")

    assert result.is_err and "missing-editor-binary" in result.unwrap_err()


def _seed_vacancy(storage: Storage) -> int:
    pipeline = async_run(storage.pipelines.create(make_pipeline()))
    vacancy = async_run(storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline.id)))
    return vacancy.id


def _seed_two_pipeline_vacancies(storage: Storage) -> tuple[int, int, int]:
    """Seed one vacancy in each of two pipelines; return (pipeline_a, vacancy_a, vacancy_b) ids."""
    pipeline_a = async_run(storage.pipelines.create(make_pipeline(name="a")))
    pipeline_b = async_run(storage.pipelines.create(make_pipeline(name="b")))
    vacancy_a = async_run(storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline_a.id, external_id="a1")))
    vacancy_b = async_run(storage.vacancies.upsert(make_vacancy(pipeline_id=pipeline_b.id, external_id="b1")))
    return pipeline_a.id, vacancy_a.id, vacancy_b.id


def test_group_and_subcommand_help_include_filtering_examples() -> None:
    for arguments in (
        ["vacancies", "--help"],
        ["vacancies", "dump", "--help"],
        ["vacancies", "apply", "--help"],
        ["vacancies", "edit", "--help"],
    ):
        result = runner.invoke(app, arguments)
        assert result.exit_code == 0, result.output
        assert "Filtering and batching" in result.output
        assert "jq" in result.output
        assert "yq" in result.output
        assert "$EDITOR" in result.output
        assert "| jobfucker vacancies apply" in result.output


def test_dump_defaults_to_yaml_on_stdout(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_services(monkeypatch, storage)
    _seed_vacancy(storage)

    result = runner.invoke(app, ["vacancies", "dump"])

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith("$schema:")
    assert "Dumped vacancies" not in result.stdout


def test_dump_format_selects_json_on_stdout(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_services(monkeypatch, storage)
    _seed_vacancy(storage)

    result = runner.invoke(app, ["vacancies", "dump", "--format", "json"])

    assert result.exit_code == 0, result.output
    document = TypeAdapter(  # lint-ignore[raw-dict]: CLI JSON boundary
        dict[str, JsonValue]
    ).validate_json(result.stdout)
    vacancies = document["vacancies"]
    assert isinstance(vacancies, dict)
    assert len(vacancies) == 1


def test_dump_output_refuses_overwrite_and_short_force_replaces(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_services(monkeypatch, storage)
    _seed_vacancy(storage)
    output_path = tmp_path / "vacancies.yaml"

    first = runner.invoke(app, ["vacancies", "dump", "--output", str(output_path)])
    refused = runner.invoke(app, ["vacancies", "dump", "--output", str(output_path)])
    forced = runner.invoke(app, ["vacancies", "dump", "--output", str(output_path), "-f"])
    unsupported = runner.invoke(app, ["vacancies", "dump", "--output", str(tmp_path / "vacancies.txt")])
    force_without_output = runner.invoke(app, ["vacancies", "dump", "-f"])

    assert first.exit_code == 0 and "rows=1" in first.output
    assert output_path.read_text(encoding="utf-8").startswith("$schema:")
    assert refused.exit_code != 0 and "already exists" in refused.output
    assert forced.exit_code == 0
    assert unsupported.exit_code != 0 and "suffix" in unsupported.output
    assert force_without_output.exit_code != 0 and "--output" in force_without_output.output


def test_dump_and_apply_accept_tilde_paths(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``--output``/``--source`` accept ``~/``-style paths (expanded against the home dir)."""
    _install_services(monkeypatch, storage)
    _seed_vacancy(storage)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    dumped = runner.invoke(app, ["vacancies", "dump", "--output", "~/vacancies.yaml"])
    applied = runner.invoke(app, ["vacancies", "apply", "--source", "~/vacancies.yaml"])

    assert dumped.exit_code == 0, dumped.output
    assert (home / "vacancies.yaml").is_file()
    assert applied.exit_code == 0, applied.output


def test_dry_run_writes_nothing_and_apply_persists_same_edit(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _install_services(monkeypatch, storage)
    vacancy_id = _seed_vacancy(storage)
    source_path = tmp_path / "vacancies.json"
    assert runner.invoke(app, ["vacancies", "dump", "--output", str(source_path)]).exit_code == 0
    source_path.write_text(
        source_path.read_text(encoding="utf-8").replace('"notes": null', '"notes": "reviewed"', 1),
        encoding="utf-8",
    )

    preview = runner.invoke(app, ["vacancies", "apply", "--source", str(source_path), "--dry-run"])
    assert preview.exit_code == 0 and "changed=1" in preview.output
    assert async_run(storage.vacancies.get(vacancy_id)).notes is None  # type: ignore[union-attr]  # rationale: seeded row must exist
    assert async_run(storage.audit_log.list()) == []

    applied = runner.invoke(app, ["vacancies", "apply", "--source", str(source_path)])
    assert applied.exit_code == 0 and "changed=1" in applied.output
    assert async_run(storage.vacancies.get(vacancy_id)).notes == "reviewed"  # type: ignore[union-attr]  # rationale: seeded row must exist
    assert len(async_run(storage.audit_log.list())) == 1


def test_apply_reads_yaml_from_stdin_by_default(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_services(monkeypatch, storage)
    vacancy_id = _seed_vacancy(storage)
    dumped = runner.invoke(app, ["vacancies", "dump"])
    assert dumped.exit_code == 0, dumped.output
    edited = dumped.stdout.replace("notes: null", "notes: reviewed", 1)

    applied = runner.invoke(app, ["vacancies", "apply"], input=edited)

    assert applied.exit_code == 0 and "changed=1" in applied.output
    assert async_run(storage.vacancies.get(vacancy_id)).notes == "reviewed"  # type: ignore[union-attr]  # rationale: seeded row must exist


def test_apply_reads_explicit_json_format_from_stdin(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_services(monkeypatch, storage)
    vacancy_id = _seed_vacancy(storage)
    dumped = runner.invoke(app, ["vacancies", "dump", "--format", "json"])
    assert dumped.exit_code == 0, dumped.output
    edited = dumped.stdout.replace('"notes": null', '"notes": "reviewed"', 1)

    applied = runner.invoke(app, ["vacancies", "apply", "--format", "json"], input=edited)

    assert applied.exit_code == 0 and "changed=1" in applied.output
    assert async_run(storage.vacancies.get(vacancy_id)).notes == "reviewed"  # type: ignore[union-attr]  # rationale: seeded row must exist


def _python_editor(script: str) -> str:
    """Cross-platform ``EDITOR`` value that runs real Python, not a shell tool.

    The EDITOR contract splits on words (``shlex``), so both the interpreter
    path and the ``-c`` script carry double quotes: POSIX ``shlex.split`` sheds
    them, the Windows path sheds exactly one pair per token. The script itself
    only uses single quotes.
    """
    return f'"{sys.executable}" -c "{script}"'


def test_edit_uses_editor_without_shell_and_applies_after_confirmation(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_services(monkeypatch, storage)
    vacancy_id = _seed_vacancy(storage)
    monkeypatch.setenv(
        "EDITOR",
        _python_editor(
            "import sys,pathlib; p=pathlib.Path(sys.argv[1]); "
            "p.write_text(p.read_text(encoding='utf-8')"
            ".replace('notes: null', 'notes: reviewed'), encoding='utf-8')"
        ),
    )

    result = runner.invoke(app, ["vacancies", "edit"], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Apply changes?" in result.output
    assert async_run(storage.vacancies.get(vacancy_id)).notes == "reviewed"  # type: ignore[union-attr]  # rationale: seeded row must exist


def test_edit_skips_confirmation_when_nothing_changed(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_services(monkeypatch, storage)
    _seed_vacancy(storage)
    monkeypatch.setenv("EDITOR", _python_editor("import sys"))

    result = runner.invoke(app, ["vacancies", "edit"])

    assert result.exit_code == 0, result.output
    assert "changed=0" in result.output
    assert "Apply changes?" not in result.output
    assert "No changes to apply." in result.output


def test_edit_requires_editor_and_preserves_invalid_content(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_services(monkeypatch, storage)
    vacancy_id = _seed_vacancy(storage)
    monkeypatch.delenv("EDITOR", raising=False)
    if os.name == "nt":
        # Windows deliberately falls back to notepad; launching a GUI app from
        # a test would hang a headless runner forever, so assert the
        # resolution directly instead of running the command.
        assert _resolve_editor_argv(None) == Ok(["notepad"])
    else:
        missing = runner.invoke(app, ["vacancies", "edit"])
        assert missing.exit_code != 0 and "EDITOR" in missing.output

    monkeypatch.setenv(
        "EDITOR",
        _python_editor(
            "import sys,pathlib; p=pathlib.Path(sys.argv[1]); "
            "p.write_text(p.read_text(encoding='utf-8')"
            ".replace('score: null', 'score: 99'), encoding='utf-8')"
        ),
    )
    invalid = runner.invoke(app, ["vacancies", "edit"])
    assert invalid.exit_code != 0
    match = re.search(r"Edited document preserved at (.+)", invalid.output)
    assert match is not None
    recovery_path = Path(match.group(1).strip())
    try:
        assert recovery_path.exists()
        if os.name == "posix":
            # POSIX-only semantics: Windows mode bits do not carry 0o600.
            assert os.stat(recovery_path).st_mode & 0o777 == 0o600
        assert "score: 99" in recovery_path.read_text(encoding="utf-8")
        assert async_run(storage.vacancies.get(vacancy_id)).score is None  # type: ignore[union-attr]  # rationale: seeded row must exist
    finally:
        recovery_path.unlink(missing_ok=True)


def test_dump_pipeline_id_scopes_rows_and_unknown_id_fails(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_services(monkeypatch, storage)
    pipeline_a_id, vacancy_a_id, _vacancy_b_id = _seed_two_pipeline_vacancies(storage)

    scoped = runner.invoke(app, ["vacancies", "dump", "--format", "json", "--pipeline-id", str(pipeline_a_id)])
    assert scoped.exit_code == 0, scoped.output
    document = TypeAdapter(  # lint-ignore[raw-dict]: CLI JSON boundary
        dict[str, JsonValue]
    ).validate_json(scoped.stdout)
    vacancies = document["vacancies"]
    assert isinstance(vacancies, dict)
    assert tuple(vacancies) == (f"vacancy_{vacancy_a_id}",)

    unknown = runner.invoke(app, ["vacancies", "dump", "--pipeline-id", "999"])
    assert unknown.exit_code != 0 and "Pipeline id 999 not found." in unknown.output


def test_dump_rejects_soft_deleted_pipeline(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_services(monkeypatch, storage)
    pipeline_a_id, _vacancy_a_id, _vacancy_b_id = _seed_two_pipeline_vacancies(storage)
    assert async_run(storage.pipelines.soft_delete(pipeline_a_id)) is True

    result = runner.invoke(app, ["vacancies", "dump", "--pipeline-id", str(pipeline_a_id)])

    assert result.exit_code != 0 and f"Pipeline id {pipeline_a_id} not found." in result.output


def test_edit_pipeline_id_edits_only_that_pipeline(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_services(monkeypatch, storage)
    pipeline_a_id, vacancy_a_id, vacancy_b_id = _seed_two_pipeline_vacancies(storage)
    monkeypatch.setenv(
        "EDITOR",
        _python_editor(
            "import sys,pathlib; p=pathlib.Path(sys.argv[1]); "
            "p.write_text(p.read_text(encoding='utf-8')"
            ".replace('notes: null', 'notes: reviewed'), encoding='utf-8')"
        ),
    )

    result = runner.invoke(app, ["vacancies", "edit", "--pipeline-id", str(pipeline_a_id)], input="y\n")

    assert result.exit_code == 0, result.output
    assert "Apply changes?" in result.output
    assert async_run(storage.vacancies.get(vacancy_a_id)).notes == "reviewed"  # type: ignore[union-attr]  # rationale: seeded row must still exist
    assert async_run(storage.vacancies.get(vacancy_b_id)).notes is None  # type: ignore[union-attr]  # rationale: seeded row must still exist


def test_apply_has_no_pipeline_flag(
    storage: Storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_services(monkeypatch, storage)
    _seed_two_pipeline_vacancies(storage)

    result = runner.invoke(app, ["vacancies", "apply", "--pipeline-id", "1"])

    assert result.exit_code != 0
