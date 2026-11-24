#!/usr/bin/env bash
# alpha-mesh/bin/run-evals.sh
# Run the review-pull-request evals on a PR and post the verdict (PMAT-312).
#
#   mesh run-evals <PR url>                              run the evals on the PR's head; post `evals` and the score table
#   mesh run-evals --smoke <PR url>                      k=1 on eval/smoke.json's cases: posts `evals-smoke` (PMAT-358)
#   mesh run-evals --force-success <PR url> --reason R   post `evals` = success by hand ("forced: R"), recorded on the PR
#
# GitHub's required check `evals-gate` (.github/workflows/evals-gate.yml) reads the `evals` status these post; each
# post re-runs that check, so it updates without a push. `gh pr checks <PR url>` shows the result.
#
# An evals run is the full eval (the development cases, k=3, every repeat judged) in a clean worktree, compared with
# master's baseline, tolerance and scoring rule (score_eval_run.py, write_baseline.py), so a PR cannot loosen its own gate. About 75 min and about
# a day's free-model quota. Run it again on the same head to resume: a run stopped by the quota continues.
#
# One eval run at a time on this machine (a lock), and only when the free-model requests left today cover it
# (OpenRouter's /api/v1/key; the quota resets each UTC day). Both are checked before any status is posted; a run
# not started for either exits 75. The quota is logged before and after each run in ~/.mesh/quota.log (PMAT-358).

set -euo pipefail

mesh_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
agent_rel="agents/review-pull-request"
pr_re='^https://github\.com/([^/]+/[^/]+)/pull/([0-9]+)$'

help_text() {
  cat <<'EOF'
mesh run-evals: run the review-pull-request evals on a PR and post the result.

Usage:
  mesh run-evals <PR url>
      The full evals on the PR's head: the development cases, k=3, every repeat judged, scored against master's
      baseline and tolerance. Posts the commit status `evals` and a PR comment with the score table.
      Takes about 75 min and about a day's free-model quota. Run it again on the same head to resume.
      On alpha-mesh, the queue's gate (mesh eval-rc-q gate) runs it on the promotion PR; see EVAL-RC-Q.md.
  mesh run-evals --smoke <PR url>
      The smoke evals: k=1 on the cases in master's eval/smoke.json, scored against the baseline's first repeat.
      Posts the commit status `evals-smoke` and a PR comment. Run it before marking an agent PR AWAITING-MERGE:
      mesh merge-pr needs evals-smoke = success to merge an agent PR into the queue.
  mesh run-evals --force-success <PR url> --reason "<why>"
      Sets `evals` = success by hand ("forced: <why>") and records the reason on the PR, for a merge you have
      decided on despite the evals.
  mesh run-evals --help
      Shows this help.

Every run waits for no other: while another mesh run-evals runs, it exits (75). It starts only when the free-model
requests left today cover it (400 for a full run, 50 for smoke), and logs them in ~/.mesh/quota.log.

Which PRs need it: those that change the review-pull-request agent's src/, eval/promptfoo/ or eval/cases/, or its
review_config.toml, pyproject.toml, uv.lock, eval/promptfooconfig.yaml or eval/holdout.json. Other PRs pass the
required check `evals-gate` without it.

See the result: gh pr checks <PR url> (the `evals-gate` check reads `evals`). `mesh merge-pr <PR url>` offers to
run the evals when they are all that blocks the merge.
More: agents/review-pull-request/eval/README.md, "The regression gate".
EOF
}
usage() {  # wrong or missing arguments: the help, on stderr
  help_text >&2
  exit 2
}

# The free-model requests a run needs left today before it starts (PMAT-358). Full: owner's starting threshold
# (2026-11-24: a full gate is about 330 file-review calls, plus 20%), until ~/.mesh/quota.log measures one gate.
# Smoke: provisional (19 cases at k=1 is about a tenth of a full run, plus margin).
MIN_FREE_FULL=400
MIN_FREE_SMOKE=50
lock_file="${MESH_RUN_EVALS_LOCK:-$HOME/.mesh/run-evals.lock}"
quota_log="$HOME/.mesh/quota.log"

# free_remaining: the free-model requests left today, from OpenRouter's /api/v1/key. The token goes on stdin, never
# on the command line or in the output.
free_remaining() {
  local token="${ALPHA_MESH_OPENROUTER_TOKEN:-}"
  [ -n "$token" ] || token="$(sed -n 's/^ALPHA_MESH_OPENROUTER_TOKEN=//p' "$mesh_dir/.env" 2>/dev/null | tr -d "\"'")"
  curl -sf --max-time 30 -H @- "${MESH_OPENROUTER_KEY_URL:-https://openrouter.ai/api/v1/key}" \
    <<<"Authorization: Bearer $token" | jq -r '.data.free_model_daily_requests.remaining // empty'
}

# start_run <mode full|smoke> <run name>: take the lock and check the quota, or exit 75 having started nothing.
# The lock is held until this script exits.
start_run() {
  local min=$MIN_FREE_FULL
  [ "$1" = smoke ] && min=$MIN_FREE_SMOKE
  mkdir -p "$(dirname "$lock_file")" "$(dirname "$quota_log")"
  exec 9>"$lock_file"
  if ! flock -n 9; then
    echo "Not run: another mesh run-evals is running (lock $lock_file). One eval runs at a time: run this again when it ends." >&2
    exit 75
  fi
  quota_before="$(free_remaining || true)"
  if ! [[ "$quota_before" =~ ^[0-9]+$ ]]; then
    echo "Not run: could not read the free-model quota left (OpenRouter /api/v1/key). Check the token in .env and the network." >&2
    exit 75
  fi
  echo "$(date -u +%FT%TZ) run=$2 mode=$1 before=$quota_before" >> "$quota_log"
  if [ "$quota_before" -lt "$min" ]; then
    echo "Not run: $quota_before free-model requests left today, below the $min a $1 run needs. The quota resets each UTC day: run it after 00:00 UTC." >&2
    exit 75
  fi
  echo "Free-model requests left today: $quota_before (a $1 run needs $min)." >&2
}
log_quota_after() {  # log_quota_after <mode> <run name>
  local after
  after="$(free_remaining || true)"
  [[ "$after" =~ ^[0-9]+$ ]] || after=unknown
  echo "$(date -u +%FT%TZ) run=$2 mode=$1 after=$after used=$([ "$after" = unknown ] && echo unknown || echo $((quota_before - after)))" >> "$quota_log"
  echo "Free-model requests left after the run: $after." >&2
}

pending_repo="" pending_sha="" pending_context=""
post_status() {  # post_status <repo> <sha> <context> <state> <description>
  gh api "repos/$1/statuses/$2" -f state="$4" -f context="$3" -f description="${5:0:140}" >/dev/null
  if [ "$4" = pending ]; then pending_repo="$1" pending_sha="$2" pending_context="$3"; else pending_sha=""; fi
}
# rerun_evals_gate <repo> <sha>: re-run the newest `evals-gate` check run on <sha>, so GitHub reads the new `evals`.
# None yet (the workflow is not on the base branch, or never ran on this head): GitHub runs it on the next push.
rerun_evals_gate() {
  local id
  id="$(gh run list -R "$1" --workflow evals-gate.yml --commit "$2" --json databaseId -q '.[0].databaseId' 2>/dev/null || true)"
  if [ -n "$id" ]; then
    gh run rerun "$id" -R "$1" >/dev/null && echo "Re-ran the evals-gate check (run $id)." >&2 && return
  fi
  echo "NOTE: no evals-gate check run on ${2:0:7} to re-run; it runs on the next push to the PR." >&2
}

# A run that stops before its verdict (a crash, a missing file) never leaves its status pending: it posts error.
on_exit() {
  local code=$?
  [ -z "$pending_sha" ] \
    || post_status "$pending_repo" "$pending_sha" "$pending_context" error \
         "mesh run-evals stopped before a verdict (exit $code): see its log" || true
  exit "$code"
}
trap on_exit EXIT

# run_gate <head> <run name> <base ref> [smoke]: run the eval at <head> and score it against <base ref>'s baseline
# and tolerance. Leaves the report in $run_dir/gate.md and the verdict in $gate (0 pass, else fail); exits on a run
# the gate could not score, after posting $context = error on <head>. Smoke runs only the cases in <base ref>'s
# eval/smoke.json, so a PR cannot shrink its own smoke set.
run_gate() {
  local head="$1" run="$2" ref="$3" mode="${4:-full}" baseline_json tolerance smoke_json=""
  local eval_args=(--judge-repeats all) check_args=()
  baseline_json="$(git -C "$mesh_dir" show "$ref:$agent_rel/eval/baseline.json")"
  if [ "$mode" = smoke ]; then
    if ! smoke_json="$(git -C "$mesh_dir" show "$ref:$agent_rel/eval/smoke.json" 2>/dev/null)"; then
      post_status "$repo" "$head" "$context" error "$ref has no eval/smoke.json"
      echo "ERROR: $ref has no $agent_rel/eval/smoke.json" >&2
      exit 1
    fi
    local pattern
    pattern="^($(jq -r '.cases | join("|")' <<<"$smoke_json"))\$"
    eval_args=(--k 1 --judge-repeats first --filter-pattern "$pattern")
  fi
  local gate_src msg
  if ! gate_src="$(git -C "$mesh_dir" show "$ref:$agent_rel/eval/scripts/score_eval_run.py" 2>/dev/null)"; then
    # A PR is judged by <base ref>'s rule only: with none there (a rename or a deletion), fail, never use the head's.
    msg="$ref has no score_eval_run.py, so no tolerance to judge by"
    [ -z "$context" ] || post_status "$repo" "$head" "$context" error "$msg"
    echo "ERROR: $msg" >&2
    exit 1
  fi
  tolerance="$(sed -n 's/^TOLERANCE = \([0-9][0-9]*\).*/\1/p' <<<"$gate_src")"
  if [ -z "$tolerance" ]; then
    echo "ERROR: no TOLERANCE in $ref's score_eval_run.py" >&2
    exit 1
  fi
  wt="${MESH_RUN_EVALS_DIR:-$HOME/environment/pmat/alpha-mesh-run-evals}/${head:0:12}"
  [ -d "$wt" ] || git -C "$mesh_dir" worktree add -q --detach "$wt" "$head"
  if [ "$(git -C "$wt" rev-parse HEAD)" != "$head" ]; then
    echo "ERROR: $wt is not at $head" >&2
    exit 1
  fi
  agent="$wt/$agent_rel"
  [ -e "$wt/.env" ] || [ -L "$wt/.env" ] || ln -s "$mesh_dir/.env" "$wt/.env"  # -L: a dangling link is there too
  mkdir -p "$HOME/.mesh/eval-runs"
  [ -e "$agent/eval/runs" ] || ln -s "$HOME/.mesh/eval-runs" "$agent/eval/runs"
  (cd "$agent" && uv sync -q)
  run_dir="$agent/eval/runs/$run"
  mkdir -p "$run_dir"
  printf '%s\n' "$baseline_json" > "$run_dir/baseline-${ref##*/}.json"
  if [ -n "$smoke_json" ]; then
    printf '%s\n' "$smoke_json" > "$run_dir/smoke-${ref##*/}.json"
    check_args=(--quick --cases "$run_dir/smoke-${ref##*/}.json")
  fi
  echo "Eval gate: ${head:0:7}, run $run, $mode, tolerance $tolerance (worktree $wt)" >&2

  # The eval's own exit code says only whether every case passed; the gate decides.
  "$agent/eval/scripts/run_eval.sh" --run "$run" "${eval_args[@]}" || true
  log_quota_after "$mode" "$run"
  gate=0
  rm -f "$run_dir/gate.md"  # a report left by an earlier gate run on this head must never be posted as this one's
  # Score with <base ref>'s rule, not the PR's: its score_eval_run.py and the write_baseline.py it counts passes with
  # replace the head's in this throwaway worktree, so a PR cannot change how its own run is scored. Restored after,
  # so a resume starts clean.
  local scorer=(eval/scripts/score_eval_run.py eval/scripts/write_baseline.py) f src
  for f in "${scorer[@]}"; do
    if src="$(git -C "$mesh_dir" show "$ref:$agent_rel/$f" 2>/dev/null)"; then printf '%s\n' "$src" > "$agent/$f"; fi
  done
  (cd "$agent" && uv run python eval/scripts/score_eval_run.py "eval/runs/$run" --baseline "$run_dir/baseline-${ref##*/}.json" \
     --tolerance "$tolerance" --markdown "$run_dir/gate.md" "${check_args[@]}") || gate=$?
  git -C "$wt" checkout -q -- "${scorer[@]/#/$agent_rel/}" || true
  if [ ! -s "$run_dir/gate.md" ]; then
    [ -z "$context" ] || post_status "$repo" "$head" "$context" error "the gate could not score run $run: see its log"
    echo "ERROR: score_eval_run.py wrote no report (exit $gate)" >&2
    exit 1
  fi
  summary="$(grep -m1 '^runs passed' "$run_dir/gate.md")"
}

remove_worktree() {
  # The run, with its checkpoints, stays in ~/.mesh/eval-runs. Unlink the links first, so nothing follows them.
  rm -f "$agent/eval/runs" "$wt/.env"
  git -C "$mesh_dir" worktree remove --force "$wt"
}

case "${1:-}" in
  -h|--help) help_text; exit 0 ;;

  --force-success)
    [ $# -eq 4 ] && [[ "$2" =~ $pr_re ]] && [ "$3" = --reason ] && [ -n "$4" ] || usage
    url="$2" repo="${BASH_REMATCH[1]}" reason="$4"
    head="$(gh pr view "$url" --json headRefOid | jq -r .headRefOid)"
    post_status "$repo" "$head" evals success "forced: $reason"
    rerun_evals_gate "$repo" "$head"
    gh pr comment "$url" --body "**evals forced to success** on \`${head:0:7}\` by hand (\`mesh run-evals --force-success\`): $reason" >/dev/null
    echo "evals = success (forced) on $head." >&2
    exit 0 ;;

  --smoke)
    [ $# -eq 2 ] && [[ "$2" =~ $pr_re ]] || usage
    url="$2" repo="${BASH_REMATCH[1]}" number="${BASH_REMATCH[2]}"
    read -r state head base < <(gh pr view "$url" --json state,headRefOid,baseRefName \
      | jq -r '"\(.state) \(.headRefOid) \(.baseRefName)"')
    [ "$state" = OPEN ] || { echo "Not run: the PR is not open (it is $state)." >&2; exit 1; }
    run="smoke-pr$number-${head:0:7}"
    start_run smoke "$run"
    git -C "$mesh_dir" fetch -q origin "$base" "pull/$number/head"
    context=evals-smoke  # never `evals`: the full gate runs on the queue (mesh eval-rc-q gate)
    post_status "$repo" "$head" evals-smoke pending "mesh run-evals --smoke is running on ${head:0:7}"
    run_gate "$head" "$run" "origin/$base" smoke
    if [ "$gate" -eq 0 ]; then
      post_status "$repo" "$head" evals-smoke success "$summary"
    else
      post_status "$repo" "$head" evals-smoke failure "$summary"
    fi
    { echo "**mesh run-evals --smoke** on \`${head:0:7}\` (run \`$run\`): the smoke evals, **not the full gate**,"
      echo "which runs on the queue eval-rc-q/current (EVAL-RC-Q.md)."; echo; cat "$run_dir/gate.md"
    } > "$run_dir/gate-comment.md"
    gh pr comment "$url" --body-file "$run_dir/gate-comment.md" >/dev/null
    cat "$run_dir/gate.md"
    remove_worktree
    exit "$gate" ;;

  -*) usage ;;
esac

[ $# -eq 1 ] && [[ "$1" =~ $pr_re ]] || usage
url="$1"
repo="${BASH_REMATCH[1]}"
number="${BASH_REMATCH[2]}"

read -r state head base < <(gh pr view "$url" --json state,headRefOid,baseRefName \
  | jq -r '"\(.state) \(.headRefOid) \(.baseRefName)"')
if [ "$state" != "OPEN" ]; then
  echo "Not run: the PR is not open (it is $state)." >&2
  exit 1
fi
behind="$(gh api "repos/$repo/compare/$base...$head" | jq -r .behind_by)"
if [ "$behind" != "0" ]; then
  echo "Not run: the branch is $behind commit(s) behind $base, so mesh merge-pr would refuse it anyway." >&2
  echo "Merge $base in, push, then run this again." >&2
  exit 1
fi

start_run full "gate-pr$number-${head:0:7}"
git -C "$mesh_dir" fetch -q origin "$base" "pull/$number/head"
context=evals
post_status "$repo" "$head" evals pending "mesh run-evals is running on ${head:0:7}"
run_gate "$head" "gate-pr$number-${head:0:7}" "origin/$base"  # master's baseline and tolerance, never the PR's

if [ "$gate" -eq 0 ]; then
  post_status "$repo" "$head" evals success "$summary"
else
  post_status "$repo" "$head" evals failure "$summary"
fi
rerun_evals_gate "$repo" "$head"
{ echo "**mesh run-evals** on \`${head:0:7}\` (run \`gate-pr$number-${head:0:7}\`)"; echo; cat "$run_dir/gate.md"; } \
  > "$run_dir/gate-comment.md"
gh pr comment "$url" --body-file "$run_dir/gate-comment.md" >/dev/null
cat "$run_dir/gate.md"
remove_worktree
if [ "$gate" -eq 0 ]; then
  echo "Gate passed: evals = success on $head." >&2
else
  echo "Gate failed: evals = failure on $head." >&2
fi
exit "$gate"
