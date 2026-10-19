#!/bin/bash
# End-to-end resume check with real models (PMAT-266): start a review, kill -9 it once file K is in
# flight, resume it with another model, and check the resume kept files 1..K-1 and names both models.
# Nothing is posted (--no-post-comment). Needs AWS credentials (checkpoint store) and gh auth.
#
# Usage: scripts/smoke-resume.sh <pr-url> <model-a> <model-b> [kill-at-file (default 2)]
# Env:   SMOKE_TIMEOUT  seconds to wait for each run (default 3600)
set -euo pipefail

if [[ $# -lt 3 ]]; then
  echo "Usage: $0 <pr-url> <model-a> <model-b> [kill-at-file (default 2)]" >&2
  exit 2
fi
PR_URL="$1"; MODEL_A="$2"; MODEL_B="$3"; KILL_AT="${4:-2}"
TIMEOUT="${SMOKE_TIMEOUT:-3600}"
if [[ "$KILL_AT" -lt 2 ]]; then
  echo "kill-at-file must be >= 2, so at least one file is resumed from the checkpoint" >&2
  exit 2
fi

OUT_DIR="${HOME}/environment/logs/ai/claude/smoke-resume-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT_DIR"
echo "=== smoke-resume: output in $OUT_DIR"

fail() { echo "FAIL: $*" >&2; exit 1; }

# Wait until the file matches the pattern; fail if the pid ends first or the timeout passes.
wait_for() {
  local file="$1" pattern="$2" pid="$3" waited=0
  until grep -qE "$pattern" "$file" 2>/dev/null; do
    kill -0 "$pid" 2>/dev/null || fail "run ended before '$pattern' (see $file)"
    (( waited >= TIMEOUT )) && fail "timed out after ${TIMEOUT}s waiting for '$pattern' (see $file)"
    sleep 5; waited=$((waited + 5))
  done
}

# --- Run 1: model A from scratch, killed while file KILL_AT is in flight ---------------------------
echo "=== run 1: $MODEL_A, kill -9 at file $KILL_AT"
setsid mesh review-pull-request "$PR_URL" --model "$MODEL_A" --no-post-comment --force-rerun \
  > "$OUT_DIR/run1.log" 2>&1 < /dev/null &
RUN1=$!
wait_for "$OUT_DIR/run1.log" "^Starting review \(thread " "$RUN1"
THREAD="$(sed -n 's/^Starting review (thread \(.*\))$/\1/p' "$OUT_DIR/run1.log")"
wait_for "$OUT_DIR/run1.log" "^\[${KILL_AT}/[0-9]+\] reviewing " "$RUN1"

# The review process holds the thread's run lock and has written its pid into it (spec 3.6a).
LOCK="${HOME}/.mesh/locks/review-pull-request-$(printf '%s' "$THREAD" | sha256sum | cut -c1-16).lock"
REVIEW_PID="$(cat "$LOCK")"
[[ "$REVIEW_PID" =~ ^[0-9]+$ ]] || fail "no pid in $LOCK"
kill -9 "$REVIEW_PID"
wait "$RUN1" 2>/dev/null || true
echo "    killed pid $REVIEW_PID (thread $THREAD)"

# --- Run 2: resume with model B ---------------------------------------------------------------------
echo "=== run 2: resume with $MODEL_B"
set +e
timeout "$TIMEOUT" mesh review-pull-request "$PR_URL" --model "$MODEL_B" --no-post-comment \
  > "$OUT_DIR/run2.log" 2>&1 < /dev/null
RC=$?
set -e
[[ $RC -eq 0 ]] || fail "resumed run exited $RC (see $OUT_DIR/run2.log)"

KEPT=$((KILL_AT - 1))
grep -q "^Resuming: ${KEPT} file(s) from checkpoint" "$OUT_DIR/run2.log" \
  || fail "expected 'Resuming: ${KEPT} file(s) from checkpoint' in run2.log"
grep -qF "**Models:**" "$OUT_DIR/run2.log" || fail "report header does not list several models"
grep -qF "(model: \`$MODEL_A\`)" "$OUT_DIR/run2.log" || fail "no file reviewed by $MODEL_A in the report"
grep -qF "(model: \`$MODEL_B\`)" "$OUT_DIR/run2.log" || fail "no file reviewed by $MODEL_B in the report"

echo "PASS: resumed from file $KILL_AT with $MODEL_B; $KEPT file(s) kept from $MODEL_A"
