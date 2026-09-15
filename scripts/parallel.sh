#!/usr/bin/env bash
# Parallel batch runs for jobfucker: N workers, worker W owns slice
# [ (W-1)*take, W*take ) — each runs <score|generate> --skip-already-processed
# until idle. Safe to re-run at any point: processed vacancies are skipped,
# failures are retried on the next pass (error rows have no result yet).
#
# Usage: parallel.sh <score|generate> <pipeline-id> [workers] [take]
#   score|generate  jobfucker subcommand (same batch CLI API)
#   workers         default 10
#   take            default 100
#
# Env (inherited by jobfucker): CONFIG_DIR, DATA_DIR.
# Logs: logs/parallel.<timestamp>/*.log — one file per worker + final sweep.

set -uo pipefail

usage() {
    cat <<'EOF'
usage: parallel.sh <score|generate> <pipeline-id> [workers] [take]

Run jobfucker score or generate in parallel across N workers, each owning a
fixed slice [ (W-1)*take, W*take ) and looping --skip-already-processed until
idle, then a final sweep retries leftovers.

  score|generate  jobfucker subcommand to run (same batch flags: --from/--take)
  pipeline-id     stored pipeline id
  workers         worker count (default 10)
  take            slice size per worker (default 100)

Env: CONFIG_DIR, DATA_DIR are inherited by jobfucker.
Logs: logs/parallel.<timestamp>/ — worker.N.log, sweep.log, run.log.
Safe to re-run; already-processed vacancies are skipped.
EOF
}

if [ $# -eq 0 ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    usage
    exit 0
fi

COMMAND="${1:?usage: parallel.sh <score|generate> <pipeline-id> [workers] [take]}"
PIPELINE_ID="${2:?usage: parallel.sh <score|generate> <pipeline-id> [workers] [take]}"
WORKERS="${3:-10}"
TAKE="${4:-100}"

case "$COMMAND" in
    score|generate) ;;
    *)
        echo "unknown command: $COMMAND (expected score or generate)" >&2
        usage >&2
        exit 2
        ;;
esac

# Idle per pass: nothing produced and nothing failed (error rows retry next pass).
case "$COMMAND" in
    score)    DONE_FIELD="scored" ;;
    generate) DONE_FIELD="generated" ;;
esac

# jobfucker streams -v events per vacancy: unbuffered stdout so log files get
# lines in progress, not in 8KB Python chunks.
export PYTHONUNBUFFERED=1

JOBFUCKER_CMD=(uv run poe app)
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$REPO_DIR"
LOGDIR="$WORKDIR/logs/parallel.$(date +%Y%m%d-%H%M%S)"
mkdir -p "$LOGDIR"

log() {
    echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOGDIR/run.log"
}

log "command=$COMMAND pipeline=$PIPELINE_ID workers=$WORKERS take=$TAKE logs=$LOGDIR"

PIDS=()
FAILED_WORKERS=0

cleanup() {
    log "interrupt received — stopping workers"
    kill "${PIDS[@]}" 2>/dev/null
    for p in "${PIDS[@]}"; do
        wait "$p" 2>/dev/null
    done
    log "interrupted. continue later by re-running; done items are skipped (--skip-already-processed)"
    exit 130
}
trap cleanup INT TERM

worker() {
    local w="$1"
    local logfile="$2"
    local from=$(((w - 1) * TAKE))
    local consecutive_failures=0
    local rc done failed
    while :; do
        "${JOBFUCKER_CMD[@]}" "$COMMAND" --pipeline-id "$PIPELINE_ID" --from "$from" --take "$TAKE" --skip-already-processed -v >>"$logfile" 2>&1
        rc=$?
        if [ "$rc" -ne 0 ]; then
            consecutive_failures=$((consecutive_failures + 1))
            if [ "$consecutive_failures" -ge 3 ]; then
                echo "worker $w: jobfucker failed $consecutive_failures times in a row — giving up" >>"$logfile"
                return 1
            fi
            continue
        fi
        consecutive_failures=0
        done="$(grep "^$COMMAND" "$logfile" | tail -1 | sed -n "s/.*$DONE_FIELD=\([0-9][0-9]*\).*/\1/p")"
        failed="$(grep "^$COMMAND" "$logfile" | tail -1 | sed -n 's/.*failed=\([0-9][0-9]*\).*/\1/p')"
        # idle when nothing left to do: no items at all, or all skipped, or
        # nothing produced and nothing failed (failed items retry on next pass)
        if [ -z "$done" ] || { [ "$done" -eq 0 ] 2>/dev/null; } || { [ -n "$done" ] && [ "$done" -eq 0 ] && [ -n "$failed" ] && [ "$failed" -eq 0 ]; }; then
            echo "worker $w: done (idle pass)" >>"$logfile"
            return 0
        fi
    done
}

for w in $(seq 1 "$WORKERS"); do
    logfile="$LOGDIR/worker.$w.log"
    : >"$logfile"
    worker "$w" "$logfile" &
    PIDS+=("$!")
done

log "started $WORKERS workers — Ctrl+C to stop (results already saved are kept)"

for p in "${PIDS[@]}"; do
    if ! wait "$p"; then
        FAILED_WORKERS=$((FAILED_WORKERS + 1))
    fi
done

log "workers finished: $((WORKERS - FAILED_WORKERS))/$WORKERS ok"

log "final sweep (retry leftovers, skip-already-processed)"
"${JOBFUCKER_CMD[@]}" "$COMMAND" --pipeline-id "$PIPELINE_ID" --skip-already-processed -v >"$LOGDIR/sweep.log" 2>&1
SWEEP_RC=$?
if [ "$SWEEP_RC" -ne 0 ]; then
    log "final sweep failed (rc=$SWEEP_RC) — rerun the script to retry; see $LOGDIR/sweep.log"
    exit 1
fi

done_total="$(grep -h "^$COMMAND" "$LOGDIR"/worker.*.log "$LOGDIR/sweep.log" 2>/dev/null | sed -E "s/.*$DONE_FIELD=([0-9]+).*/\1/" | awk '{s+=$1} END{print s+0}')"
failed_total="$(grep -h "^$COMMAND" "$LOGDIR"/worker.*.log "$LOGDIR/sweep.log" 2>/dev/null | sed -E 's/.*failed=([0-9]+).*/\1/' | awk '{s+=$1} END{print s+0}')"
log "done. $DONE_FIELD (passes sum)=$done_total failed (passes sum)=$failed_total logs=$LOGDIR"
log "note: sums count repeated passes; per-vacancy truth lives in the DB. failed>0 → rerun script."

exit "$FAILED_WORKERS"