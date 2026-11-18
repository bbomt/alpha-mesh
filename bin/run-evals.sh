#!/usr/bin/env bash
# alpha-mesh/bin/run-evals.sh
# Run the review-pull-request evals on a PR and post the verdict (PMAT-312).
#
#   mesh run-evals <PR url>                              run the evals on the PR's head; post `evals` and the score table
#   mesh run-evals --quick <PR url>                      a k=1 early warning (a third of the quota): a PR comment, no status
#   mesh run-evals --force-success <PR url> --reason R   post `evals` = success by hand ("forced: R"), recorded on the PR
#
# GitHub's required check `evals-gate` (.github/workflows/evals-gate.yml) reads the `evals` status these post; each
# post re-runs that check, so it updates without a push. `gh pr checks <PR url>` shows the result.
#
# An evals run is the full eval (the development cases, k=3, every repeat judged) in a clean worktree, compared with
# master's baseline, tolerance and scoring rule (score_eval_run.py, write_baseline.py), so a PR cannot loosen its own gate. About 75 min and about
# a day's free-model quota. Run it again on the same head to resume: a run stopped by the quota continues.

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
  mesh run-evals --quick <PR url>
      An early warning only (k=1, a third of the quota): a PR comment, no `evals` status, so it never lets a PR
      merge. Run it before marking an agent PR AWAITING-MERGE.
  mesh run-evals --force-success <PR url> --reason "<why>"
      Sets `evals` = success by hand ("forced: <why>") and records the reason on the PR, for a merge you have
      decided on despite the evals.
  mesh run-evals --help
      Shows this help.

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

# run_gate <head> <run name> <base ref> [quick]: run the eval at <head> and score it against <base ref>'s baseline
# and tolerance. Leaves the report in $run_dir/gate.md and the verdict in $gate (0 pass, else fail); exits on a run
# the gate could not score, after posting $context = error on <head> (unless $context is empty: the quick check).
run_gate() {
  local head="$1" run="$2" ref="$3" mode="${4:-}" baseline_json tolerance
  local eval_args=(--judge-repeats all) check_args=()
  [ "$mode" = quick ] && eval_args=(--k 1 --judge-repeats first) check_args=(--quick)
  baseline_json="$(git -C "$mesh_dir" show "$ref:$agent_rel/eval/baseline.json")"
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
  [ -e "$wt/.env" ] || ln -s "$mesh_dir/.env" "$wt/.env"
  mkdir -p "$HOME/.mesh/eval-runs"
  [ -e "$agent/eval/runs" ] || ln -s "$HOME/.mesh/eval-runs" "$agent/eval/runs"
  (cd "$agent" && uv sync -q)
  run_dir="$agent/eval/runs/$run"
  mkdir -p "$run_dir"
  printf '%s\n' "$baseline_json" > "$run_dir/baseline-${ref##*/}.json"
  echo "Eval gate: ${head:0:7}, run $run, tolerance $tolerance (worktree $wt)" >&2

  # The eval's own exit code says only whether every case passed; the gate decides.
  "$agent/eval/scripts/run_eval.sh" --run "$run" "${eval_args[@]}" || true
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

  --quick)
    [ $# -eq 2 ] && [[ "$2" =~ $pr_re ]] || usage
    url="$2" repo="${BASH_REMATCH[1]}" number="${BASH_REMATCH[2]}"
    read -r state head base < <(gh pr view "$url" --json state,headRefOid,baseRefName \
      | jq -r '"\(.state) \(.headRefOid) \(.baseRefName)"')
    [ "$state" = OPEN ] || { echo "Not run: the PR is not open (it is $state)." >&2; exit 1; }
    git -C "$mesh_dir" fetch -q origin "$base" "pull/$number/head"
    context=""  # an early warning only: it never posts evals, so it can never let a PR merge
    run_gate "$head" "quick-pr$number-${head:0:7}" "origin/$base" quick
    { echo "**mesh run-evals --quick** on \`${head:0:7}\`: an early warning (k=1), **not the gate**. It catches only a"
      echo "large drop; the full gate runs at merge (\`mesh merge-pr\` offers it)."; echo; cat "$run_dir/gate.md"
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
