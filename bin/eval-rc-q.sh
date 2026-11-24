#!/usr/bin/env bash
# alpha-mesh/bin/eval-rc-q.sh
# mesh eval-rc-q: the batch queue eval-rc-q/current (PMAT-358). The runbook, with every step's manual equivalent and
# the recovery steps, is EVAL-RC-Q.md.
#
#   mesh eval-rc-q status        the queue's state, the PRs in its batch and the promotion PR
#   mesh eval-rc-q gate          gate the batch: label eval-rc:gating, merge master in if it moved, run the tests,
#                                run the full evals on the promotion PR, then promote (pass) or label eval-rc:fixing
#   mesh eval-rc-q gate --due    the nightly cron's call: gate a filling queue that holds a PR, or resume a gate
#   mesh eval-rc-q promote       merge the gated promotion PR into master (a merge commit at the gated sha), tag
#                                it eval-rc-q-<date>-<n> and delete the queue branch
#
# While the label is eval-rc:gating or eval-rc:fixing, mesh merge-pr merges nothing into the queue, so the gated head
# never moves: a gate stopped by the quota (exit 75) resumes on the next gate of the same head.

set -euo pipefail

mesh_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
status_report="$mesh_dir/agents/review-pull-request/eval/scripts/check_pr_evals.py"
# shellcheck source=bin/lib/merge-queue.sh
source "$mesh_dir/bin/lib/merge-queue.sh"

help_text() {
  sed -n '3,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}
usage() { help_text >&2; exit 2; }

# The queue's repo: the one repo in config/merge-queue.json (PMAT-359 may add more queues, not more repos).
repo="${MESH_EVAL_RC_Q_REPO:-$(jq -r '.queues | keys | first // empty' "$mq_config")}"
[ -n "$repo" ] || { echo "ERROR: no queue in $mq_config" >&2; exit 1; }
mq_load "$repo"

# next_tag: eval-rc-q-<UTC date>-<n>, n counting the promotions already tagged that day.
next_tag() {
  local day n
  day="$(date -u +%F)"
  n="$(gh api "repos/$repo/git/matching-refs/tags/eval-rc-q-$day-" | jq length)"
  echo "eval-rc-q-$day-$((n + 1))"
}

comment() {  # comment <markdown>: on the promotion PR
  gh pr comment "$mq_promo_url" --body "$1" >/dev/null
}

fix_it() {  # fix_it <why>: label eval-rc:fixing, say why on the promotion PR and here, exit 1
  mq_set_state "$repo" fixing
  comment "**mesh eval-rc-q gate: eval-rc:fixing.** $1 Nothing merges into the queue until it is cleared: see EVAL-RC-Q.md, \"Fix\"."
  echo "Gate failed: $1" >&2
  echo "The queue is eval-rc:fixing: nothing merges into it until it is cleared (EVAL-RC-Q.md, \"Fix\"). Promotion PR: $mq_promo_url" >&2
  exit 1
}

run_tests() {  # run_tests <sha>: the repo's tests at <sha>, in a throwaway worktree
  if [ -n "${MESH_EVAL_RC_Q_TESTS:-}" ]; then "$MESH_EVAL_RC_Q_TESTS" "$1"; return; fi
  local wt="${MESH_RUN_EVALS_DIR:-$HOME/environment/pmat/alpha-mesh-run-evals}/tests-${1:0:12}" rc=0
  git -C "$mesh_dir" fetch -q origin "$mq_branch"
  [ -d "$wt" ] || git -C "$mesh_dir" worktree add -q --detach "$wt" "$1"
  (cd "$wt" && bash test/run-all.sh) || rc=$?
  git -C "$mesh_dir" worktree remove --force "$wt"
  return "$rc"
}

status() {
  mq_promo "$repo"
  if ! mq_branch_exists "$repo"; then
    echo "No queue: $mq_branch does not exist. The next mesh merge-pr creates it from $mq_default."
    return
  fi
  local cmp subjects n
  cmp="$(mq_compare "$repo")"
  subjects="$(mq_batch_subjects "$cmp")"
  n="$(grep -c . <<<"$subjects" || true)"
  echo "Queue $mq_branch: ${mq_state:-no promotion PR} · $n/$mq_batch PRs · $mq_default moved since: $(jq -r '.behind_by' <<<"$cmp") commit(s)"
  echo "Promotion PR: ${mq_promo_url:-none (created by the next mesh merge-pr)}"
  [ -z "$subjects" ] || sed 's/^/  /' <<<"$subjects"
}

promote() {
  mq_promo "$repo"
  [ -n "$mq_promo_url" ] || { echo "ERROR: no open promotion PR for $mq_branch" >&2; exit 1; }
  local report sha subjects tickets prs tag subject merge_sha
  if ! report="$(python3 "$status_report" --evals-only "$mq_promo_url")"; then
    echo "Not promoted: the evals have not passed on the promotion PR's head." >&2
    echo "$report" >&2
    exit 1
  fi
  sha="$(gh pr view "$mq_promo_url" --json headRefOid | jq -r .headRefOid)"
  subjects="$(mq_batch_subjects "$(mq_compare "$repo")")"
  tickets="$(grep -oE '[A-Z][A-Z0-9]+-[0-9]+' <<<"$subjects" | awk '!seen[$0]++' | paste -sd, - | sed 's/,/, /g')"
  prs="$(grep -oE '\(#[0-9]+\)$' <<<"$subjects" | tr -d '()' | paste -sd, - | sed 's/,/, /g')"
  tag="$(next_tag)"
  subject="chore[$tag]: promoted $tickets ($prs)"
  gh pr ready "$mq_promo_url" >/dev/null
  gh pr checks "$mq_promo_url" --required --watch >/dev/null 2>&1 || true  # the merge below fails if one failed
  gh pr merge "$mq_promo_url" --merge --match-head-commit "$sha" --subject "$subject" \
    --body "Promoted the batch gated on $sha (mesh eval-rc-q, EVAL-RC-Q.md)."
  merge_sha="$(gh pr view "$mq_promo_url" --json mergeCommit | jq -r .mergeCommit.oid)"
  if [ "$(gh api "repos/$repo/commits/$merge_sha" | jq -r .commit.tree.sha)" != "$(gh api "repos/$repo/commits/$sha" | jq -r .commit.tree.sha)" ]; then
    echo "ERROR: $mq_default's tree at $merge_sha is not the gated tree at $sha. Not tagged; the queue branch is kept." >&2
    echo "See EVAL-RC-Q.md, \"Recovery\"." >&2
    exit 1
  fi
  gh api "repos/$repo/git/refs" -f ref="refs/tags/$tag" -f sha="$merge_sha" >/dev/null
  # A PR still targeting the queue would close with it: move it back to the default branch first.
  local u
  for u in $(gh pr list -R "$repo" --base "$mq_branch" --state open --json url | jq -r '.[].url'); do
    gh pr edit "$u" --base "$mq_default" >/dev/null
    echo "Moved $u back to $mq_default (it targeted the queue)." >&2
  done
  gh api -X DELETE "repos/$repo/git/refs/heads/$mq_branch" >/dev/null
  echo "Promoted: $subject" >&2
  echo "$mq_default is at $merge_sha (tag $tag); $mq_branch is deleted, and the next mesh merge-pr starts a new queue." >&2
}

gate() {
  local due="${1:-}" n cmp sha rc=0 state
  mq_promo "$repo"
  if [ -z "$mq_promo_url" ]; then
    [ -n "$due" ] && { echo "Nothing to gate: no queue."; exit 0; }
    echo "ERROR: no open promotion PR for $mq_branch: nothing to gate." >&2
    exit 1
  fi
  n="$(mq_count "$repo")"
  if [ -n "$due" ] && ! { [ "$mq_state" = filling ] && [ "$n" -gt 0 ]; } && [ "$mq_state" != gating ]; then
    echo "Nothing due: the queue is $mq_state with $n PR(s)."
    exit 0
  fi
  [ "$n" -gt 0 ] || { echo "ERROR: the queue holds no PR: nothing to gate." >&2; exit 1; }
  mq_set_state "$repo" gating
  echo "Gating $mq_branch ($n PRs): merges into the queue are frozen. Promotion PR: $mq_promo_url" >&2

  cmp="$(mq_compare "$repo")"
  if [ "$(jq -r .behind_by <<<"$cmp")" != 0 ]; then
    if ! gh api -X POST "repos/$repo/merges" -f base="$mq_branch" -f head="$mq_default" \
         -f commit_message="chore[$(next_tag)]: merged $mq_default before the gate" >/dev/null; then
      fix_it "Merging $mq_default into $mq_branch conflicts."
    fi
    echo "Merged $mq_default into $mq_branch." >&2
  fi
  sha="$(gh pr view "$mq_promo_url" --json headRefOid | jq -r .headRefOid)"
  echo "Gated head: $sha. Running the tests." >&2
  run_tests "$sha" || fix_it "The tests failed on \`${sha:0:7}\`."

  if python3 "$status_report" --evals-only "$mq_promo_url" >/dev/null; then
    echo "The evals are not needed (no agent file in the batch) or have passed on $sha." >&2
  else
    "${MESH_RUN_EVALS:-$mesh_dir/bin/run-evals.sh}" "$mq_promo_url" || rc=$?
    if [ "$rc" -eq 75 ]; then
      echo "Gate paused before the evals finished (exit 75: another run or the quota). The label stays eval-rc:gating;" >&2
      echo "mesh eval-rc-q gate (or tonight's gate --due) resumes it on the same head $sha." >&2
      exit 75
    fi
    state="$(gh api "repos/$repo/commits/$sha/status" | jq -r '[.statuses[] | select(.context == "evals")][0].state // "missing"')"
    case "$state" in
      success) ;;
      failure) fix_it "The full evals failed on \`${sha:0:7}\` (the report is the mesh run-evals comment above)." ;;
      *) echo "Gate stopped without a verdict (evals: $state). The label stays eval-rc:gating: rerun mesh eval-rc-q gate to resume." >&2
         exit 1 ;;
    esac
  fi
  promote
}

case "${1:-}" in
  -h|--help) help_text; exit 0 ;;
  status) [ $# -eq 1 ] || usage; status ;;
  promote) [ $# -eq 1 ] || usage; promote ;;
  gate)
    if [ $# -eq 1 ]; then gate
    elif [ $# -eq 2 ] && [ "$2" = --due ]; then gate due
    else usage; fi ;;
  *) usage ;;
esac
