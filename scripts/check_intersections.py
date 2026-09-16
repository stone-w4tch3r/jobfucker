#!/usr/bin/env python3
"""Detect cross-pipeline apply-candidate intersections (read-only).

Hardcoded for the three real HH pipelines:
  3 hh-dbus      — Node/Python fullstack persona
  4 hh-dmesgx    — C#/.NET fullstack persona
  5 hh-systemctl — AI engineering + fullstack persona

Overlap rule (user decision, 2026-09-01): dbus and systemctl MAY both apply to
the same vacancy; dmesgx must NOT overlap either of them. Any vacancy where
dmesgx's candidate set intersects dbus/systemctl is a CONFLICT — resolution
(skipping one side) is a separate manual step, this script only detects.

Detection replicates the apply stage's strict eligibility (stages/apply.py
``_ignore_reason``): not manual_skip, score >= MIN_SCORE, apply_status not
decided, cover letter present. State is read via ``jobfucker vacancies dump``
(versioned document schema) — never writes to the DB.

Exit code: 0 when no dmesgx conflicts, 1 when any are found (for future
automation gates; the report always prints).
"""

# =============================================================================
# Constants & Types
# =============================================================================

import argparse
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml

REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# (pipeline id, display name) — hardcoded per user decision.
PIPELINES: Final[tuple[tuple[str, str], ...]] = (
    ("3", "hh-dbus"),
    ("4", "hh-dmesgx"),
    ("5", "hh-systemctl"),
)

# All three pipeline configs use min_required_score: 3 (2026-09-01).
MIN_SCORE: Final[int] = 3

# Allowed overlap pairs (frozenset of pipeline ids) — dbus x systemctl.
ALLOWED_OVERLAPS: Final[tuple[frozenset[str], ...]] = (frozenset({"3", "5"}),)

# The pipeline that must never overlap the others.
EXCLUDED_PIPELINE: Final[str] = "4"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One apply-eligible vacancy in a pipeline."""

    handle: str
    score: int
    title: str


@dataclass(frozen=True, slots=True)
class Conflict:
    """A vacancy dmesgx wants to apply to alongside another pipeline."""

    external_id: str
    title: str
    scores: dict[str, int]


# =============================================================================
# Utils & Helpers
# =============================================================================


def _as_dict(
    value: object,
) -> dict[
    str, object
]:  # lint-ignore[restricted-object,raw-dict]: untrusted YAML boundary; every node re-narrowed before use
    """Narrow an untrusted YAML value to a mapping (dump schema is known)."""
    if not isinstance(value, dict):
        sys.exit(f"unexpected dump document shape: {type(value).__name__}")
    return {str(k): v for k, v in value.items()}  # type: ignore[reportUnknownVariableType]  # rationale: third-party yaml boundary; keys coerced, values re-narrowed by callers


def _as_int(
    value: object,
) -> int:  # lint-ignore[restricted-object]: untrusted YAML value narrowed by isinstance before use
    """Narrow an untrusted YAML value to an int score."""
    if not isinstance(value, int) or isinstance(value, bool):
        sys.exit(f"unexpected score value: {value!r}")
    return value


def dump_candidates(
    pipeline_id: str,
) -> dict[str, Candidate]:  # lint-ignore[raw-dict]: keyed-by-external_id candidate table
    """Dump one pipeline and return its apply candidates keyed by external_id."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "dump.yaml"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "jobfucker",
                "vacancies",
                "dump",
                "--pipeline-id",
                pipeline_id,
                "--format",
                "yaml",
                "--output",
                str(out),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            sys.exit(f"vacancies dump failed for pipeline {pipeline_id}:\n{result.stderr}")
        doc = _as_dict(yaml.safe_load(out.read_text()))  # type: ignore[reportUnknownArgument,reportAny]  # rationale: third-party yaml boundary; _as_dict re-validates every node

    candidates: dict[str, Candidate] = {}
    for handle, row in _as_dict(doc["vacancies"]).items():
        row_doc = _as_dict(row)
        editable = _as_dict(row_doc["editable"])
        context = _as_dict(row_doc["context_edit_not_allowed"])
        score = editable.get("score")
        apply_status = context.get("apply_status")
        if (
            not editable.get("manual_skip")
            and score is not None
            and _as_int(score) >= MIN_SCORE
            and apply_status not in ("applied", "skipped", "error")
            and editable.get("cover_letter") is not None
        ):
            key = context["external_id"]
            if not isinstance(key, str):
                sys.exit(f"unexpected external_id value for row {handle}: {key!r}")
            candidates[key] = Candidate(handle=handle, score=_as_int(score), title=str(context["title"]))
    return candidates


# =============================================================================
# Business Logic
# =============================================================================


def find_conflicts(
    candidates_by_pipeline: dict[str, dict[str, Candidate]],
) -> list[Conflict]:  # lint-ignore[raw-dict]: pipeline-id-keyed candidate tables
    """Vacancies where the EXCLUDED pipeline overlaps any other pipeline."""
    by_external_id: dict[str, dict[str, Candidate]] = {}
    for pipeline_id, candidates in candidates_by_pipeline.items():
        for external_id, candidate in candidates.items():
            by_external_id.setdefault(external_id, {})[pipeline_id] = candidate

    conflicts: list[Conflict] = []
    for external_id, by_pipeline in by_external_id.items():
        pipelines = frozenset(by_pipeline)
        if EXCLUDED_PIPELINE not in pipelines:
            continue
        others = pipelines - {EXCLUDED_PIPELINE}
        if not others:
            continue
        if any(pipelines == allowed for allowed in ALLOWED_OVERLAPS):
            continue
        conflicts.append(
            Conflict(
                external_id=external_id,
                title=by_pipeline[next(iter(by_pipeline))].title,
                scores={pid: cand.score for pid, cand in by_pipeline.items()},
            )
        )
    return conflicts


# =============================================================================
# CLI Interface
# =============================================================================


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.parse_args()

    candidates_by_pipeline = {pid: dump_candidates(pid) for pid, _name in PIPELINES}
    conflicts = find_conflicts(candidates_by_pipeline)

    for pipeline_id, name in PIPELINES:
        count = len(candidates_by_pipeline[pipeline_id])
        print(f"pipeline {pipeline_id} {name}: {count} apply candidates")

    dbus_ids = set(candidates_by_pipeline["3"])
    systemctl_ids = set(candidates_by_pipeline["5"])
    allowed_overlap = len(dbus_ids & systemctl_ids)
    print(f"allowed overlap 3 x 5 (both apply): {allowed_overlap}")

    print(f"conflicts (dmesgx overlapping dbus/systemctl): {len(conflicts)}")
    for conflict in sorted(conflicts, key=lambda c: c.external_id):
        scores = ", ".join(f"{pid}={score}" for pid, score in sorted(conflict.scores.items()))
        print(f"  ext {conflict.external_id} | {scores} | {conflict.title}")

    sys.exit(1 if conflicts else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
