#!/usr/bin/env bash
# alpha-mesh/bin/lib/merge-queue.sh
# The batch queue (PMAT-358, EVAL-RC-Q.md): what mesh merge-pr and mesh eval-rc-q both read. Source it.
#
# The queue's state lives on GitHub only: the branch (eval-rc-q/current), and its one open draft promotion PR into
# the default branch, labelled eval-rc:filling, eval-rc:gating or eval-rc:fixing. Every gh answer goes through jq
# (never gh's own --jq), so a test's stand-in gh only has to print JSON.

mq_config="${MESH_MERGE_QUEUE_CONFIG:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/config/merge-queue.json}"
MQ_STATES=(filling gating fixing)

# mq_load <repo>: sets mq_branch, mq_default and mq_batch for <repo>; mq_branch is empty when it has no queue.
mq_load() {
  local q
  q="$(jq -c --arg r "$1" '.queues[$r] // {}' "$mq_config")"
  mq_branch="$(jq -r '.branch // empty' <<<"$q")"
  mq_default="$(jq -r '.default // "master"' <<<"$q")"
  mq_batch="$(jq -r '.batch_size // 4' <<<"$q")"
}

# mq_promo <repo>: sets mq_promo_number, mq_promo_url and mq_state (filling|gating|fixing, or "unlabelled") from the
# queue's open promotion PR; all empty when there is none.
mq_promo() {
  local pr
  pr="$(gh pr list -R "$1" --head "$mq_branch" --base "$mq_default" --state open --json number,url,labels \
        | jq -c '.[0] // empty')"
  mq_promo_number="" mq_promo_url="" mq_state=""
  [ -n "$pr" ] || return 0
  mq_promo_number="$(jq -r .number <<<"$pr")"
  mq_promo_url="$(jq -r .url <<<"$pr")"
  mq_state="$(jq -r '[.labels[].name | select(startswith("eval-rc:")) | ltrimstr("eval-rc:")] | first // "unlabelled"' <<<"$pr")"
}

# mq_branch_exists <repo>: whether the queue branch exists on GitHub.
mq_branch_exists() {
  gh api "repos/$1/branches/$mq_branch" >/dev/null 2>&1
}

# mq_compare <repo>: GitHub's compare of the default branch with the queue (behind_by, commits).
mq_compare() {
  gh api "repos/$1/compare/$mq_default...$mq_branch"
}

# mq_batch_subjects <compare json>: one line per ticket in the batch: its squash commit's subject. Merge commits
# (master merged in before a gate) and reverts are not tickets.
mq_batch_subjects() {
  jq -r '.commits[] | select((.parents | length) == 1) | .commit.message | split("\n")[0]
         | select(startswith("Revert") | not)' <<<"$1"
}

# mq_count <repo>: the number of tickets in the batch (0 when the queue branch does not exist).
mq_count() {
  mq_branch_exists "$1" || { echo 0; return; }
  mq_batch_subjects "$(mq_compare "$1")" | grep -c . || true
}

# mq_set_state <repo> <state>: label the promotion PR <state>, removing the other eval-rc:* labels.
mq_set_state() {
  local args=(--add-label "eval-rc:$2") s
  for s in "${MQ_STATES[@]}"; do [ "$s" = "$2" ] || args+=(--remove-label "eval-rc:$s"); done
  gh pr edit "$mq_promo_url" "${args[@]}" >/dev/null
  mq_state="$2"
}
