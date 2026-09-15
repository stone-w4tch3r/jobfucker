"""Serialization for ``jobfucker hh-tests dump`` (async/bulk solving transport).

The dump command writes one JSON document mapping vacancy id -> the fetched
test (meta + tasks), which a human edits or an offline AI pass fills into the
answers file consumed by ``apply --test-answers``. The dump and the answers
file are separate documents: the dump carries questions + options, the answers
file carries the answers (see :mod:`jobfucker.hh_tests.file`).
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, RootModel

from jobfucker.hh_tests.contract import HhTestProblem, HhTestTaskKind

__all__ = ["HhTestDumpRecord", "dump_problems_to_json"]


@dataclass(frozen=True, slots=True)
class HhTestDumpRecord:
    """One dumped test with its vacancy display meta (input to the serializer)."""

    problem: HhTestProblem
    title: str
    url: str


class HhTestDumpOption(BaseModel):
    """One answer option in the dump document."""

    id: str
    label: str


class HhTestDumpTask(BaseModel):
    """One task in the dump document."""

    id: str
    kind: HhTestTaskKind
    prompt: str
    options: list[HhTestDumpOption]


class HhTestDumpMeta(BaseModel):
    """Vacancy display meta (so a human editing the file sees which job it is)."""

    title: str
    url: str


class HhTestDumpEntry(BaseModel):
    """One test in the dump document."""

    meta: HhTestDumpMeta
    name: str
    description: str | None = None
    tasks: list[HhTestDumpTask]


class HhTestDumpDocument(RootModel[dict[str, HhTestDumpEntry]]):
    """The whole dump document: ``vacancy_id -> test``.

    Split the same way as the answers file so the human-facing JSON is
    self-describing; the answers file keys match these vacancy ids exactly.
    """

    def to_json(self) -> str:
        """Render hand-editable JSON (stable insertion order, no nulls)."""
        return self.model_dump_json(indent=2, exclude_none=True) + "\n"


def dump_problems_to_json(records: tuple[HhTestDumpRecord, ...]) -> str:
    """Render the dump document: vacancy id -> {meta, name, description, tasks}."""
    entries: dict[str, HhTestDumpEntry] = {}
    for record in records:
        problem = record.problem
        entries[str(problem.vacancy_id)] = HhTestDumpEntry(
            meta=HhTestDumpMeta(title=record.title, url=record.url),
            name=problem.name,
            description=problem.description,
            tasks=[
                HhTestDumpTask(
                    id=task.id,
                    kind=task.kind,
                    prompt=task.prompt,
                    options=[HhTestDumpOption(id=option.id, label=option.label) for option in task.options],
                )
                for task in problem.tasks
            ],
        )
    return HhTestDumpDocument(entries).to_json()
