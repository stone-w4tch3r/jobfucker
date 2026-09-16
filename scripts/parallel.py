"""Parallel batch runs for jobfucker: N workers, worker W owns slice
[ (W-1)*take, W*take ) — each runs <score|generate> --skip-already-processed
until idle. Safe to re-run at any point: processed vacancies are skipped,
failures are retried on the next pass (error rows have no result yet).

Logs: logs/parallel.<YYYYmmdd-HHMMSS>/ — one file per worker, a final sweep
log, and run.log for orchestration events (teed to stdout).

Interrupts (Ctrl+C / SIGTERM) set a stop flag, best-effort terminate each
worker's `uv` wrapper, and exit 130. Workers share the terminal's process
group, so an interactive Ctrl+C reaches the jobfucker children directly —
terminate() covers detached starts and stragglers. Results already saved are
kept, so re-running resumes safely.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import Final

REPO_DIR: Final[Path] = Path(__file__).resolve().parent.parent
DEFAULT_WORKERS: Final = 10
DEFAULT_TAKE: Final = 100
MAX_CONSECUTIVE_FAILURES: Final = 3
INTERRUPTED_EXIT_CODE: Final = 130
# Idle per pass: nothing produced and nothing failed (error rows retry next pass).
DONE_FIELD: Final[dict[str, str]] = {"score": "scored", "generate": "generated"}
INTERRUPTED_MESSAGE: Final = (
    "interrupted. continue later by re-running; done items are skipped (--skip-already-processed)"
)
NOTE_MESSAGE: Final = "note: sums count repeated passes; per-vacancy truth lives in the DB. failed>0 → rerun script."


def log_run_event(logdir: Path, message: str) -> None:
    """Timestamped orchestration event, teed to stdout and run.log."""
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    with (logdir / "run.log").open("a", encoding="utf-8") as run_log:
        run_log.write(line + "\n")


def append_log_line(path: Path, line: str) -> None:
    with path.open("a", encoding="utf-8") as worker_log:
        worker_log.write(line + "\n")


def extract_last_field(line: str, field_pattern: re.Pattern[str]) -> int | None:
    """Value of the field's LAST occurrence in the line (bash sed is greedy),
    or None when the line has no match — mirroring `sed -n s/...//p`."""
    # finditer (not findall) for typed matches under reportAny.
    last_match: re.Match[str] | None = None
    for match in field_pattern.finditer(line):
        last_match = match
    return int(last_match.group(1)) if last_match else None


def last_pass_produced(log_path: Path, line_prefix: str, done_field: str) -> int | None:
    """`done` value from the LAST log line starting with the command name —
    the whole file is scanned (not just the current pass) exactly like the
    bash `grep ^command | tail -1`, so a pass without a summary line falls
    back to the previous pass's numbers instead of looking idle."""
    field_pattern = re.compile(rf"{done_field}=(\d+)")
    produced: int | None = None
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(line_prefix):
            produced = extract_last_field(line, field_pattern)
    return produced


@dataclass(slots=True)
class ParallelRunner:
    """Owns one parallel batch run: worker threads, child registry, logs."""

    command: str
    pipeline_id: str
    workers: int
    take: int
    logdir: Path
    child_env: dict[str, str] = field(init=False)
    base_args: list[str] = field(init=False)
    interrupt_event: threading.Event = field(default_factory=threading.Event, init=False)
    children_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    live_children: set[subprocess.Popen[bytes]] = field(default_factory=set[subprocess.Popen[bytes]], init=False)

    def __post_init__(self) -> None:
        # jobfucker streams -v events per vacancy: unbuffered stdout so log
        # files get lines in progress, not in 8KB Python chunks.
        self.child_env = os.environ | {"PYTHONUNBUFFERED": "1"}
        self.base_args = ["uv", "run", "poe", "app", self.command, "--pipeline-id", self.pipeline_id]

    @property
    def done_field(self) -> str:
        return DONE_FIELD[self.command]

    def run_pass(self, args: list[str], stdout_path: Path) -> int:
        """Run one jobfucker pass, appending its output to stdout_path."""
        with stdout_path.open("ab") as pass_log:
            child = subprocess.Popen(  # noqa: S603 — operator-supplied args, no shell
                args, cwd=REPO_DIR, env=self.child_env, stdout=pass_log, stderr=subprocess.STDOUT
            )
        # Registered under the lock so the interrupt handler can terminate it;
        # the child keeps its own duplicated stdout handle after the with-block.
        with self.children_lock:
            self.live_children.add(child)
        try:
            return child.wait()
        finally:
            with self.children_lock:
                self.live_children.discard(child)

    def run_worker(self, worker_num: int, from_offset: int, log_path: Path) -> int:
        consecutive_failures = 0
        while not self.interrupt_event.is_set():
            args = [
                *self.base_args,
                "--from",
                str(from_offset),
                "--take",
                str(self.take),
                "--skip-already-processed",
                "-v",
            ]
            returncode = self.run_pass(args, log_path)
            # A child killed by the interrupt handler is not a jobfucker failure.
            if self.interrupt_event.is_set():
                return 0
            if returncode != 0:
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    append_log_line(
                        log_path,
                        f"worker {worker_num}: jobfucker failed {consecutive_failures} times in a row — giving up",
                    )
                    return 1
                continue
            consecutive_failures = 0
            produced = last_pass_produced(log_path, self.command, self.done_field)
            # idle when nothing left to do: no summary line yet, or nothing
            # produced this pass. (The bash check also demanded failed=0, but
            # that clause is dead — done==0 short-circuits before it.)
            if produced is None or produced == 0:
                append_log_line(log_path, f"worker {worker_num}: done (idle pass)")
                return 0
        return 0

    def sum_pass_field(self, log_paths: list[Path], field_name: str) -> int:
        """Sum a per-pass field across every command summary line — repeated
        passes each contribute their (last) value, like the bash grep|sed|awk."""
        field_pattern = re.compile(rf"{field_name}=(\d+)")
        total = 0
        for log_path in log_paths:
            for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if not line.startswith(self.command):
                    continue
                value = extract_last_field(line, field_pattern)
                if value is not None:
                    total += value
        return total

    def handle_interrupt(self, signum: int, frame: FrameType | None) -> None:
        log_run_event(self.logdir, "interrupt received — stopping workers")
        self.interrupt_event.set()
        with self.children_lock:
            for child in self.live_children:
                child.terminate()

    def run(self) -> int:
        self.logdir.mkdir(parents=True, exist_ok=True)
        log_run_event(
            self.logdir,
            f"command={self.command} pipeline={self.pipeline_id} workers={self.workers}"
            f" take={self.take} logs={self.logdir}",
        )

        signal.signal(signal.SIGINT, self.handle_interrupt)
        signal.signal(signal.SIGTERM, self.handle_interrupt)

        exit_codes = [0] * self.workers

        def spawn_worker(worker_num: int) -> threading.Thread:
            log_path = self.logdir / f"worker.{worker_num}.log"
            from_offset = (worker_num - 1) * self.take

            def target() -> None:
                try:
                    exit_codes[worker_num - 1] = self.run_worker(worker_num, from_offset, log_path)
                except Exception:
                    # Thread boundary: an unexpected crash must count as a
                    # failed worker (bash counted the subshell exit code),
                    # never silently report "ok" and run the sweep anyway.
                    with log_path.open("a", encoding="utf-8") as worker_log:
                        worker_log.write(traceback.format_exc())
                    exit_codes[worker_num - 1] = 1

            return threading.Thread(target=target, name=f"worker-{worker_num}")

        worker_threads = [spawn_worker(worker_num) for worker_num in range(1, self.workers + 1)]
        for thread in worker_threads:
            thread.start()

        log_run_event(
            self.logdir,
            f"started {self.workers} workers — Ctrl+C to stop (results already saved are kept)",
        )
        # Windows cannot interrupt a blocking thread.join() with SIGINT, so
        # poll: the main thread must keep taking bytecode slices for the
        # signal handler to run on every platform.
        while any(thread.is_alive() for thread in worker_threads):
            for thread in worker_threads:
                thread.join(timeout=0.2)

        if self.interrupt_event.is_set():
            log_run_event(self.logdir, INTERRUPTED_MESSAGE)
            return INTERRUPTED_EXIT_CODE

        failed_workers = sum(1 for exit_code in exit_codes if exit_code != 0)
        log_run_event(self.logdir, f"workers finished: {self.workers - failed_workers}/{self.workers} ok")

        log_run_event(self.logdir, "final sweep (retry leftovers, skip-already-processed)")
        sweep_log = self.logdir / "sweep.log"
        sweep_returncode = self.run_pass([*self.base_args, "--skip-already-processed", "-v"], sweep_log)
        if self.interrupt_event.is_set():
            log_run_event(self.logdir, INTERRUPTED_MESSAGE)
            return INTERRUPTED_EXIT_CODE
        if sweep_returncode != 0:
            log_run_event(
                self.logdir,
                f"final sweep failed (rc={sweep_returncode}) — rerun the script to retry; see {sweep_log}",
            )
            return 1

        all_logs = [*sorted(self.logdir.glob("worker.*.log")), sweep_log]
        done_total = self.sum_pass_field(all_logs, self.done_field)
        failed_total = self.sum_pass_field(all_logs, "failed")
        log_run_event(
            self.logdir,
            f"done. {self.done_field} (passes sum)={done_total} failed (passes sum)={failed_total} logs={self.logdir}",
        )
        log_run_event(self.logdir, NOTE_MESSAGE)
        return failed_workers


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parallel.py",
        description=(
            "Run jobfucker score/generate across N slice workers until idle, then a final sweep retries leftovers."
        ),
    )
    parser.add_argument("command", choices=sorted(DONE_FIELD), help="jobfucker subcommand (same batch flags)")
    parser.add_argument("pipeline_id", help="stored pipeline id")
    parser.add_argument("workers", nargs="?", type=_positive_int, default=DEFAULT_WORKERS, help="worker count")
    parser.add_argument("take", nargs="?", type=_positive_int, default=DEFAULT_TAKE, help="slice size per worker")
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logdir = REPO_DIR / "logs" / f"parallel.{timestamp}"
    runner = ParallelRunner(
        # argparse's Namespace fields are Any under reportAny; runtime types
        # are already guaranteed by the parser (choices + _positive_int).
        command=args.command,  # type: ignore[reportAny]  # rationale: Namespace is Any; choices= enforces str
        pipeline_id=args.pipeline_id,  # type: ignore[reportAny]  # rationale: Namespace is Any; plain str positional
        workers=args.workers,  # type: ignore[reportAny]  # rationale: Namespace is Any; type=_positive_int validates
        take=args.take,  # type: ignore[reportAny]  # rationale: Namespace is Any; type=_positive_int validates
        logdir=logdir,
    )
    return runner.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
