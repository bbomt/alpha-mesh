#!/usr/bin/env bash
# alpha-mesh/bin/merge-pr.sh
# mesh merge-pr <PR url>: the owner's merge.
#
# A repo with a batch queue (config/merge-queue.json; alpha-mesh: eval-rc-q/current, PMAT-358, EVAL-RC-Q.md):
# the PR goes into the queue, not master. Refused while the queue is gating or being fixed (clear it first) or full.
# An agent PR needs `evals-smoke` = success on its head (mesh run-evals --smoke). The queue branch and its draft
# promotion PR are created on the first merge after a promotion. The PR's base moves to the queue, a PR that
# conflicts with the queue is refused (and moved back), and it is squash-merged there. The queue's gate (mesh
# eval-rc-q gate) runs the full evals once for the whole batch and promotes it into master.
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
  # ── Into the queue ────────────────────────────────────────────────────────────────────────────────────────────
  if [ "$base" != "$mq_default" ] && [ "$base" != "$mq_branch" ]; then
    echo "Not merged: the PR's base is $base; a PR into the queue starts from $mq_default." >&2
    exit 1
  fi
  mq_promo "$repo"
  if [ -n "$mq_state" ] && [ "$mq_state" != filling ]; then
    echo "Not merged: the queue $mq_branch is $mq_state: clear it first (EVAL-RC-Q.md). The PR waits at AWAITING-MERGE." >&2
    echo "Promotion PR: $mq_promo_url" >&2
    exit 1
  fi
  count="$(mq_count "$repo")"
  if [ "$count" -ge "$mq_batch" ]; then
    echo "Not merged: the queue $mq_branch is full ($count/$mq_batch PRs): gate it first (mesh eval-rc-q gate)." >&2
    [ -z "$mq_promo_url" ] || echo "Promotion PR: $mq_promo_url" >&2
    exit 1
  fi
  # The smoke evals: an agent PR needs evals-smoke = success (on its head, or carried from an older commit).
  if ! report="$(python3 "$status_report" --evals-only --smoke "$url")"; then
    echo "Not merged:" >&2
    echo "$report" >&2
    exit 1
  fi

  if ! mq_branch_exists "$repo"; then
    sha="$(gh api "repos/$repo/git/ref/heads/$mq_default" | jq -r .object.sha)"
    gh api "repos/$repo/git/refs" -f ref="refs/heads/$mq_branch" -f sha="$sha" >/dev/null
    echo "Created the queue $mq_branch from $mq_default (${sha:0:7})." >&2
  fi
  [ "$base" = "$mq_branch" ] || gh pr edit "$url" --base "$mq_branch" >/dev/null
  # GitHub computes mergeable in the background after the base changes: UNKNOWN until it has.
  mergeable=UNKNOWN
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    mergeable="$(gh pr view "$url" --json mergeable | jq -r .mergeable)"
    [ "$mergeable" = UNKNOWN ] || break
    sleep "${MESH_MERGE_POLL_SECONDS:-3}"
  done
  if [ "$mergeable" != MERGEABLE ]; then
    gh pr edit "$url" --base "$mq_default" >/dev/null
    echo "Not merged: the PR conflicts with the queue $mq_branch (GitHub: $mergeable); its base is back to $mq_default." >&2
    echo "Ask the worker to merge origin/$mq_branch into the branch, resolve, rerun its tests and push (EVAL-RC-Q.md)." >&2
    exit 1
  fi
  if ! gh pr merge "$url" --squash --match-head-commit "$head"; then
    gh pr edit "$url" --base "$mq_default" >/dev/null || true
    echo "Not merged: the squash merge into $mq_branch failed (above); the PR's base is back to $mq_default." >&2
    exit 1
  fi
  if [ -z "$mq_promo_url" ]; then
    mq_promo_url="$(gh pr create -R "$repo" --draft --base "$mq_default" --head "$mq_branch" --label eval-rc:filling \
      --title "eval-rc-q: promote the queued batch into $mq_default" \
      --body "The queue $mq_branch (EVAL-RC-Q.md). Its commits are the batch; its label is the queue's state. \`mesh eval-rc-q gate\` runs the full evals here and promotes it with a merge commit." \
      | tail -n 1)"
  fi
  count=$((count + 1))
  echo "Merged $url into the queue $mq_branch (squash, at $head): $count/$mq_batch PRs." >&2
  echo "Promotion PR: $mq_promo_url" >&2
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
