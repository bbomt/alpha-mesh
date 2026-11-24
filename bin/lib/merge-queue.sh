#!/usr/bin/env bash
# alpha-mesh/bin/lib/merge-queue.sh
# The batch queue (PMAT-358 v2, EVAL-RC-Q.md): what mesh merge-pr and mesh eval-rc-q both use. Source it.
#
# The queue only tests. Ticket PRs stay open against the default branch; the branch eval-rc-q/current is the default
# branch plus one commit per queued PR, in order: that PR's diff merged onto the queue (git merge-tree, no working
# tree), with the PR's title and (#N) as subject and the trailers "PR: <n>" and "PR-Head: <sha>". The trailers are
# the batch. The queue's state is a label on its draft batch PR (eval-rc-q/current into the default branch, never
# merged): eval-rc:filling or eval-rc:gating. Every gh answer goes through jq (never gh's own --jq), so a test's
# stand-in gh only has to print JSON.

mq_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mq_config="${MESH_MERGE_QUEUE_CONFIG:-$mq_lib_dir/../../config/merge-queue.json}"
# The checkout whose `origin` is the repo: the main checkout (the queue is built in its object store, never its tree).
mq_repo_dir="${MESH_EVAL_RC_Q_REPO_DIR:-$(cd "$mq_lib_dir/../.." && pwd)}"
MQ_STATES=(filling gating)

mq_git() { git -C "$mq_repo_dir" "$@"; }

# mq_load <repo>: sets mq_branch, mq_default and mq_batch for <repo>; mq_branch is empty when it has no queue.
mq_load() {
  local q
  q="$(jq -c --arg r "$1" '.queues[$r] // {}' "$mq_config")"
  mq_branch="$(jq -r '.branch // empty' <<<"$q")"
  mq_default="$(jq -r '.default // "master"' <<<"$q")"
  mq_batch="$(jq -r '.batch_size // 4' <<<"$q")"
}

# mq_fetch: origin's default branch and queue branch, into refs/remotes/origin/*. A queue branch that no longer exists
# on origin is removed locally too.
mq_fetch() {
  mq_git fetch -q --prune origin "+refs/heads/$mq_default:refs/remotes/origin/$mq_default" \
    "+refs/heads/$mq_branch*:refs/remotes/origin/$mq_branch*"
}
mq_queue_exists() { mq_git rev-parse -q --verify "refs/remotes/origin/$mq_branch" >/dev/null; }
mq_tip() { mq_git rev-parse "refs/remotes/origin/$mq_branch"; }
mq_master() { mq_git rev-parse "refs/remotes/origin/$mq_default"; }

# mq_members: one line per queued PR, in queue order: "<queue commit> <pr> <pr head> <subject>".
mq_members() {
  mq_queue_exists || return 0
  mq_git log --reverse --format='%H %(trailers:key=PR,valueonly,separator=) %(trailers:key=PR-Head,valueonly,separator=) %s' \
    "refs/remotes/origin/$mq_default..refs/remotes/origin/$mq_branch"
}
mq_count() { mq_members | grep -c . || true; }

# mq_base_current: whether the queue sits directly on the default branch's tip (false after a hotfix moved it).
mq_base_current() {
  local n
  n="$(mq_count)"
  [ "$n" -gt 0 ] || return 0
  [ "$(mq_git rev-parse "refs/remotes/origin/$mq_branch~$n")" = "$(mq_master)" ]
}

# mq_fetch_pr <n>: fetch PR <n>'s head; prints its sha.
mq_fetch_pr() {
  mq_git fetch -q origin "+refs/pull/$1/head:refs/eval-rc-q/pr/$1"
  mq_git rev-parse "refs/eval-rc-q/pr/$1"
}

# mq_add <tip> <pr> <pr head> <title>: the commit that adds the PR's diff onto <tip>; prints it. Fails (merging
# nothing) when the PR conflicts with <tip>.
mq_add() {
  local tree
  tree="$(mq_git merge-tree --write-tree "$1" "$3" 2>/dev/null | head -n 1)" || return 1
  printf '%s (#%s)\n\nPR: %s\nPR-Head: %s\n' "$4" "$2" "$2" "$3" | mq_git commit-tree "$tree" -p "$1"
}

# mq_push <new tip> <old tip or "">: move the queue branch, only if it is still at <old tip> (empty: must not exist).
mq_push() {
  mq_git push -q --force-with-lease="refs/heads/$mq_branch:$2" origin "$1:refs/heads/$mq_branch"
}
mq_delete_branch() { mq_git push -q origin --delete "$mq_branch"; }

# mq_batch_pr <repo>: sets mq_pr_number, mq_pr_url and mq_state (filling|gating, or "unlabelled") from the queue's open
# batch PR; all empty when there is none. A gh failure fails the caller (pipefail): never read as "no batch PR".
mq_batch_pr() {
  local pr
  pr="$(gh pr list -R "$1" --head "$mq_branch" --base "$mq_default" --state open --json number,url,labels \
        | jq -c '.[0] // empty')"
  mq_pr_number="" mq_pr_url="" mq_state=""
  [ -n "$pr" ] || return 0
  mq_pr_number="$(jq -r .number <<<"$pr")"
  mq_pr_url="$(jq -r .url <<<"$pr")"
  mq_state="$(jq -r '[.labels[].name | select(startswith("eval-rc:")) | ltrimstr("eval-rc:")] | first // "unlabelled"' <<<"$pr")"
}

# mq_set_state <repo> <state>: label the batch PR <state>, removing the other eval-rc:* labels.
mq_set_state() {
  local args=(--add-label "eval-rc:$2") s
  for s in "${MQ_STATES[@]}"; do [ "$s" = "$2" ] || args+=(--remove-label "eval-rc:$s"); done
  gh pr edit "$mq_pr_url" "${args[@]}" >/dev/null
  mq_state="$2"
}

# mq_open_batch_pr <repo>: open the draft batch PR (the queue branch must have a commit).
mq_open_batch_pr() {
  mq_pr_url="$(gh pr create -R "$1" --draft --base "$mq_default" --head "$mq_branch" --label eval-rc:filling \
    --title "eval-rc-q: the queued batch (tests only, never merged)" \
    --body "The queue $mq_branch (EVAL-RC-Q.md): $mq_default plus one commit per queued PR. Never merged: \`mesh eval-rc-q gate\` runs the full evals here, drops failing PRs, then squash-merges each passing PR into $mq_default and closes this PR." \
    | tail -n 1)"
  mq_state=filling
}

# mq_rerun_check <repo> <sha>: re-run the newest evals-gate check run on <sha>, so GitHub reads a new status.
mq_rerun_check() {
  local id
  id="$(gh run list -R "$1" --workflow evals-gate.yml --commit "$2" --json databaseId 2>/dev/null | jq -r '.[0].databaseId // empty' || true)"
  [ -z "$id" ] || gh run rerun "$id" -R "$1" >/dev/null || true
}
