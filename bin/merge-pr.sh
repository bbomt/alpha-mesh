#!/usr/bin/env bash
# alpha-mesh/bin/merge-pr.sh
# mesh merge-pr <PR url>: the owner's merge.
#
# A repo with a batch queue (config/merge-queue.json; alpha-mesh: eval-rc-q/current, PMAT-358 v2, EVAL-RC-Q.md):
# the PR is *queued*, not merged. Its diff is added as one commit onto the queue branch; the PR stays open against
# master. Refused while the queue is gating, when it is full, for an agent PR without `evals-smoke` = success, and when
# the PR conflicts with the queue (it waits for the next batch). The queue's gate (mesh eval-rc-q gate) runs the full
# evals once for the batch, drops failing PRs, then squash-merges each passing PR into master.
#
# A hotfix/* branch, or a repo with no queue: as before. Squash-merges the PR only if it is open and its branch
# already has the latest commit of its base branch, so nothing merges onto a base it was not tested against.
# A PR that changes a file the review-pull-request evals gate covers also needs `evals` = success on its head commit.
#
# --match-head-commit makes either merge fail if the branch moves between the check and the merge.

set -euo pipefail

if [ $# -ne 1 ] || [[ ! "$1" =~ ^https://github\.com/([^/]+/[^/]+)/pull/[0-9]+$ ]]; then
  echo "usage: mesh merge-pr https://github.com/<owner>/<repo>/pull/<number>" >&2
  exit 2
fi
url="$1"
repo="${BASH_REMATCH[1]}"
mesh_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
status_report="$mesh_dir/agents/review-pull-request/eval/scripts/check_pr_evals.py"
# shellcheck source=bin/lib/merge-queue.sh
source "$mesh_dir/bin/lib/merge-queue.sh"
at_terminal() { [ -t 0 ] || [ -n "${MESH_ASSUME_TTY:-}" ]; }

read -r state head base head_ref < <(gh pr view "$url" --json state,headRefOid,baseRefName,headRefName \
  | jq -r '"\(.state) \(.headRefOid) \(.baseRefName) \(.headRefName)"')
if [ "$state" != "OPEN" ]; then
  echo "Not merged: the PR is not open (it is $state)." >&2
  exit 1
fi

mq_load "$repo"
if [ -n "$mq_branch" ] && [[ "$head_ref" != hotfix/* ]]; then
  # ── Queue it ──────────────────────────────────────────────────────────────────────────────────────────────
  number="${url##*/}"
  if [ "$base" != "$mq_default" ]; then
    echo "Not queued: the PR's base is $base; a ticket PR targets $mq_default." >&2
    exit 1
  fi
  mq_batch_pr "$repo"
  if [ "$mq_state" = gating ]; then
    echo "Not queued: the queue $mq_branch is gating. The PR waits at AWAITING-MERGE; queue it after the gate (EVAL-RC-Q.md)." >&2
    echo "Batch PR: $mq_pr_url" >&2
    exit 1
  fi
  mq_fetch
  if mq_members | awk -v n="$number" '$2 == n {found=1} END {exit !found}'; then
    echo "Already queued: $url is in $mq_branch. Batch PR: $mq_pr_url" >&2
    exit 0
  fi
  count="$(mq_count)"
  if [ "$count" -ge "$mq_batch" ]; then
    echo "Not queued: the queue $mq_branch is full ($count/$mq_batch PRs): gate it first (mesh eval-rc-q gate)." >&2
    [ -z "$mq_pr_url" ] || echo "Batch PR: $mq_pr_url" >&2
    exit 1
  fi
  # The smoke evals: an agent PR needs evals-smoke = success (on its head, or carried from an older commit).
  if ! report="$(python3 "$status_report" --evals-only --smoke "$url")"; then
    echo "Not queued:" >&2
    echo "$report" >&2
    exit 1
  fi
  fetched="$(mq_fetch_pr "$number")"
  if [ "$fetched" != "$head" ]; then
    echo "Not queued: the PR moved while queuing it ($head, now $fetched). Run this again." >&2
    exit 1
  fi
  old=""; tip="$(mq_master)"
  if mq_queue_exists; then old="$(mq_tip)"; tip="$old"; fi
  title="$(gh pr view "$url" --json title | jq -r .title)"
  if ! commit="$(mq_add "$tip" "$number" "$head" "$title")"; then
    echo "Not queued: the PR conflicts with the queue $mq_branch. It waits for the next batch: after this batch is" >&2
    echo "promoted, the worker merges $mq_default in, resolves, reruns its tests and pushes (EVAL-RC-Q.md)." >&2
    exit 1
  fi
  mq_push "$commit" "$old"
  [ -n "$mq_pr_url" ] || mq_open_batch_pr "$repo"
  count=$((count + 1))
  echo "Queued $url ($count/$mq_batch PRs in $mq_branch, at $head). The PR stays open: do not push to it now." >&2
  echo "Batch PR: $mq_pr_url" >&2
  # A batch with no agent file needs no evals and costs no quota: gate it at once (tests only), never at night.
  gated="$(mq_git diff --name-only "$(mq_git merge-base "$(mq_master)" "$commit")" "$commit" \
    | python3 "$mesh_dir/agents/review-pull-request/eval/scripts/list_evals_gated_files.py")"
  if [ -z "$gated" ]; then
    echo "No queued PR changes an agent file: gating now (tests only)." >&2
    exec "${MESH_EVAL_RC_Q:-$mesh_dir/bin/eval-rc-q.sh}" gate
  fi
  if [ "$count" -ge "$mq_batch" ]; then
    if at_terminal; then
      printf "Queue full: gate it now? [y/N] " >&2
      read -r answer || answer=""
      if [[ "$answer" =~ ^[Yy]$ ]]; then exec "${MESH_EVAL_RC_Q:-$mesh_dir/bin/eval-rc-q.sh}" gate; fi
    fi
    echo "The queue is full: run mesh eval-rc-q gate (or it gates tonight)." >&2
  fi
  exit 0
fi

# ── Straight into the base: a hotfix/* branch, or a repo with no queue ──────────────────────────────────────────
behind="$(gh api "repos/$repo/compare/$base...$head" | jq -r .behind_by)"
if [ "$behind" != "0" ]; then
  echo "Not merged: stale. The branch is $behind commit(s) behind $base." >&2
  echo "Ask the worker to merge $base in, rerun its tests and push; then run this again." >&2
  exit 1
fi

# The review-pull-request evals gate (PMAT-312, check_pr_evals.py): a PR that changes a file the gate
# covers needs `evals` = success on its head, or on an older commit with no gated file changed since. GitHub enforces
# the same rule (the required check `evals-gate`); checking here first gives the report and the offer below.
# When the evals are all that is missing and the owner is at a terminal, offer to run them now: the full run is
# expensive, so it runs once the rest of the ticket's code is accepted, at merge time.
if ! report="$(python3 "$status_report" "$url")"; then
  echo "Not merged:" >&2
  echo "$report" >&2
  if grep -Fqx "Next: mesh run-evals $url" <<<"$report" && at_terminal; then
    printf "The evals have not passed on this head. Run them now (about 75 min, about a day's free-model quota), then merge? [y/N] " >&2
    read -r answer || answer=""
    [[ "$answer" =~ ^[Yy]$ ]] || exit 1
    "${MESH_RUN_EVALS:-$mesh_dir/bin/run-evals.sh}" "$url" || { echo "Not merged: the evals failed." >&2; exit 1; }
    report="$(python3 "$status_report" "$url")" || { echo "Not merged:" >&2; echo "$report" >&2; exit 1; }
  else
    exit 1
  fi
fi

gh pr merge "$url" --squash --match-head-commit "$head"
echo "Merged $url (squash, at $head, which includes the latest $base)."
