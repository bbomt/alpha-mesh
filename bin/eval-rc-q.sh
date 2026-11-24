#!/usr/bin/env bash
# alpha-mesh/bin/eval-rc-q.sh
# mesh eval-rc-q: the batch queue eval-rc-q/current (PMAT-358 v2). The runbook, with every step's manual equivalent,
# is EVAL-RC-Q.md.
#
#   mesh eval-rc-q status        the queue's state, its PRs, and Jira notes for dropped PRs still to post
#   mesh eval-rc-q gate          gate the batch until it is green: rebuild it if master moved, run the tests, run the
#                                full evals; on a failure confirm it (fluke check), bisect, drop the first bad PR and
#                                gate again; then squash-merge each passing PR into master, in order
#   mesh eval-rc-q gate --due    the nightly cron's call: gate a filling queue that holds a PR, or resume a gate
#   mesh eval-rc-q promote       promote a batch whose evals have already passed (after a crash)
#
# While the batch PR is labelled eval-rc:gating, mesh merge-pr queues nothing, so the gated head stays fixed: a gate
# stopped by the quota or the lock (exit 75) resumes on the next gate of the same head.

set -euo pipefail

mesh_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
scripts="$mesh_dir/agents/review-pull-request/eval/scripts"
# shellcheck source=bin/lib/merge-queue.sh
source "$mesh_dir/bin/lib/merge-queue.sh"
run_evals="${MESH_RUN_EVALS:-$mesh_dir/bin/run-evals.sh}"
runs_dir="${MESH_EVAL_RUNS:-$HOME/.mesh/eval-runs}"
state_dir="${MESH_EVAL_RC_Q_STATE:-$HOME/.mesh/eval-rc-q}"

help_text() { sed -n '3,15p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
usage() { help_text >&2; exit 2; }
note() { echo "$*" >&2; }

# The queue's repo: the one repo in config/merge-queue.json (PMAT-359 may add more queues, not more repos).
repo="${MESH_EVAL_RC_Q_REPO:-$(jq -r '.queues | keys | first // empty' "$mq_config")}"
[ -n "$repo" ] || { echo "ERROR: no queue in $mq_config" >&2; exit 1; }
mq_load "$repo"
pr_url() { echo "https://github.com/$repo/pull/$1"; }

next_tag() {  # eval-rc-q-<UTC date>-<n>, n counting the batches already tagged that day
  local day n
  day="$(date -u +%F)"
  n="$(gh api "repos/$repo/git/matching-refs/tags/eval-rc-q-$day-" | jq length)"
  echo "eval-rc-q-$day-$((n + 1))"
}

# ── The batch ────────────────────────────────────────────────────────────────────────────────────────────────────
# members: the queued PRs, "<queue commit> <pr> <pr head> <subject>" per line, read fresh from origin.
members=() skip=()   # skip: PRs dropped or already merged during this gate, left out of every rebuild
merged_any=0         # whether this gate merged a PR into the default branch
load_members() { mq_fetch; mapfile -t members < <(mq_members); }
skipped() { local s; for s in "${skip[@]}"; do [ "$s" = "$1" ] && return 0; done; return 1; }

# drop <pr> <why>: out of the batch. A PR comment, and a Jira note to post (this script has no Jira access).
drop() {
  local n="$1" why="$2" url title ticket body
  url="$(pr_url "$n")"
  title="$(gh pr view "$url" --json title | jq -r .title)"
  ticket="$( { grep -oE '[A-Z][A-Z0-9]+-[0-9]+' <<<"$title" || true; } | head -n 1)"
  body="**Dropped from the eval-rc-q batch** (${mq_pr_url}): $why

The PR stays open and nothing of it reached $mq_default. Fix it on this branch (merge $mq_default in), run \`mesh run-evals --smoke\` again if it is an agent PR, then mark it ready to merge again (EVAL-RC-Q.md, \"Dropped\")."
  gh pr comment "$url" --body "$body" >/dev/null
  mkdir -p "$state_dir/drops"
  { echo "Jira: ${ticket:-its ticket}   PR: $url"; echo; echo "$body"; } > "$state_dir/drops/$n.md"
  skip+=("$n")
  note "Dropped #$n: $why"
  note "To post on Jira ${ticket:-(its ticket)}: $state_dir/drops/$n.md (delete it once posted)."
}

# pr_state <pr>: "<state> <head>" from GitHub. A command substitution, never a process substitution, so a gh
# failure stops the script (set -e) instead of reading as an empty state.
pr_state() { gh pr view "$(pr_url "$1")" --json state,headRefOid | jq -r '"\(.state) \(.headRefOid)"'; }

# check_members: drop a PR that moved or closed since it was queued; skip one already merged (a promotion that
# stopped partway). Sets changed=1 when the batch changed, so it needs a rebuild. Called plainly (never in an `if`),
# so set -e holds inside it.
check_members() {
  local line c n h s st cur info
  changed=0
  for line in "${members[@]}"; do
    read -r c n h s <<<"$line"
    if skipped "$n"; then changed=1; continue; fi
    info="$(pr_state "$n")"
    read -r st cur <<<"$info"
    if [ "$st" = MERGED ]; then skip+=("$n"); changed=1
    elif [ "$st" != OPEN ]; then drop "$n" "it was closed after it was queued."; changed=1
    elif [ "$cur" != "$h" ]; then drop "$n" "it moved after it was queued (queued at \`${h:0:7}\`, now \`${cur:0:7}\`): a queued PR must not change."; changed=1
    else mq_fetch_pr "$n" >/dev/null; fi
  done
}

finish_empty() {  # exits 0 when this gate merged a PR, 1 when every PR was dropped
  gh pr comment "$mq_pr_url" --body "**mesh eval-rc-q gate:** no PR left in the batch (every one was dropped or merged). Closing it." >/dev/null
  gh pr close "$mq_pr_url" >/dev/null
  if mq_queue_exists; then mq_delete_branch; fi
  note "The queue is empty: closed the batch PR and deleted $mq_branch."
  [ "$merged_any" -eq 1 ]
  exit
}

# rebuild: the queue again on the default branch's tip, without the skipped PRs. A PR that no longer applies is dropped.
rebuild() {
  local old tip line c n h s new
  old="$(mq_tip)" tip="$(mq_master)"
  for line in "${members[@]}"; do
    read -r c n h s <<<"$line"
    skipped "$n" && continue
    if new="$(mq_add "$tip" "$n" "$h" "${s% (#"$n")}")"; then tip="$new"
    else drop "$n" "it conflicts with $mq_default or with the PRs before it in the queue, so it cannot be gated with them."; fi
  done
  [ "$tip" != "$(mq_master)" ] || finish_empty
  mq_push "$tip" "$old"
  note "Rebuilt $mq_branch on $mq_default (${tip:0:7})."
}

# ── Checks ───────────────────────────────────────────────────────────────────────────────────────────────────────
run_tests() {  # run_tests <sha>: the repo's tests at <sha>, in a throwaway worktree
  if [ -n "${MESH_EVAL_RC_Q_TESTS:-}" ]; then "$MESH_EVAL_RC_Q_TESTS" "$1"; return; fi
  local wt="${MESH_RUN_EVALS_DIR:-$HOME/environment/pmat/alpha-mesh-run-evals}/tests-${1:0:12}" rc=0
  # Called in an `if`: set -e is off here, so a worktree that cannot be made stops the script, never reads as a
  # test failure (that would drop a PR).
  [ -d "$wt" ] || mq_git worktree add -q --detach "$wt" "$1" || { note "ERROR: cannot make a worktree at $1"; exit 1; }
  (cd "$wt" && bash test/run-all.sh) >&2 || rc=$?
  mq_git worktree remove --force "$wt"
  return "$rc"
}
gated_files() {  # gated_files <sha>: the files of the batch at <sha> the evals gate covers. Assign it (never in an
                 # `if`), so a failing git or python stops the script instead of reading as "no evals needed".
  mq_git diff --name-only "$(mq_master)" "$1" | python3 "$scripts/list_evals_gated_files.py"
}
evals_state() {  # evals_state <sha>: the `evals` status on <sha>
  gh api "repos/$repo/commits/$1/status" | jq -r '[.statuses[] | select(.context == "evals")][0].state // "missing"'
}
cases_ok() {  # cases_ok <sha>: the drop loop's cases pass at <sha> (0), fail (1); exits 75 if not started
  local rc=0
  "$run_evals" --cases "$cases_file" --commit "$1" --run "$2-pr$mq_pr_number-${1:0:7}" >&2 || rc=$?
  [ "$rc" -ne 75 ] || { note "Gate paused (exit 75: another run or the quota). The label stays eval-rc:gating: mesh eval-rc-q gate resumes it."; exit 75; }
  return "$rc"
}
tests_ok() { run_tests "$1"; }

# first_bad <check> <run label>: sets bad to the 1-based index of the first queue commit at which <check> fails,
# knowing it fails at the last. Binary search: 4 PRs take at most 2 checks. Not in a subshell, so a check's exit 75
# (paused) ends the script.
first_bad() {
  local lo=1 hi="${#members[@]}" mid c
  while [ "$lo" -lt "$hi" ]; do
    mid=$(( (lo + hi) / 2 ))
    read -r c _ <<<"${members[$((mid - 1))]}"
    if "$1" "$c" "$2"; then lo=$((mid + 1)); else hi=$mid; fi
  done
  bad="$lo"
}
member_pr() { local c n; read -r c n _ <<<"${members[$(( $1 - 1 ))]}"; echo "$n"; }

attempt_file() { echo "$state_dir/attempts/$1"; }
attempt_of() { cat "$(attempt_file "$1")" 2>/dev/null || echo 1; }

# ── Promote ──────────────────────────────────────────────────────────────────────────────────────────────────────
# promote: squash-merge each queued PR into the default branch, in order. Sets promoted=0 when the batch changed (a
# PR moved, or no longer matches the gated tree): the caller rebuilds and gates again. Called plainly, so set -e
# holds and any failed gh call stops it.
promote() {
  local tip tag line c n h s url st cur behind new desc i master info files
  promoted=1
  tip="$(mq_tip)" tag="$(next_tag)"
  # Every PR first, before any merge: one that changed sends the whole batch back to the gate, so master never gets
  # a prefix of the batch for that reason. (A tree that differs after GitHub's update-branch can only be seen at
  # that PR's turn: then the PRs before it are already merged; EVAL-RC-Q.md, "Recovery".)
  for line in "${members[@]}"; do
    read -r c n h s <<<"$line"
    info="$(pr_state "$n")"
    read -r st cur <<<"$info"
    if [ "$st" != OPEN ] || [ "$cur" != "$h" ]; then
      drop "$n" "it changed before its promotion (state $st, head \`${cur:0:7}\`)."; promoted=0; return 0
    fi
  done
  for line in "${members[@]}"; do
    read -r c n h s <<<"$line"
    url="$(pr_url "$n")" cur="$h"
    behind="$(gh api "repos/$repo/compare/$mq_default...$cur" | jq -r .behind_by)"
    new="$cur"
    if [ "$behind" != 0 ]; then  # the master ruleset requires a branch up to date: GitHub merges master into it
      gh api -X PUT "repos/$repo/pulls/$n/update-branch" -f expected_head_sha="$cur" >/dev/null
      for i in $(seq 1 40); do
        new="$(gh pr view "$url" --json headRefOid | jq -r .headRefOid)"
        [ "$new" = "$cur" ] || break
        sleep "${MESH_MERGE_POLL_SECONDS:-3}"
      done
      [ "$new" != "$cur" ] || { note "ERROR: GitHub did not update #$n's branch with $mq_default in time. Run mesh eval-rc-q promote again."; exit 1; }
    fi
    if [ "$(gh api "repos/$repo/commits/$new" | jq -r .commit.tree.sha)" != "$(mq_git rev-parse "$c^{tree}")" ]; then
      drop "$n" "updated with $mq_default, its tree is not the tree that was gated for it, so it is not what passed."
      promoted=0; return 0
    fi
    desc="gated in batch $tag at ${tip:0:7}"
    gh api "repos/$repo/statuses/$new" -f state=success -f context=eval-rc-q -f description="$desc" >/dev/null
    files="$(gh pr diff "$url" --name-only | python3 "$scripts/list_evals_gated_files.py")"
    if [ -n "$files" ]; then
      gh api "repos/$repo/statuses/$new" -f state=success -f context=evals -f description="$desc" >/dev/null
    fi
    mq_rerun_check "$repo" "$new"
    gh pr comment "$url" --body "**Gated in eval-rc-q batch $tag:** the tests and the full evals passed on the batch at \`${tip:0:7}\` ($mq_pr_url). Squash-merging." >/dev/null
    gh pr checks "$url" --required --watch >/dev/null 2>&1 || true  # the merge below fails if a required check failed
    gh pr merge "$url" --squash --match-head-commit "$new"
    skip+=("$n") merged_any=1
    note "Merged #$n into $mq_default."
  done
  master="$(gh api "repos/$repo/git/ref/heads/$mq_default" | jq -r .object.sha)"
  if [ "$(gh api "repos/$repo/commits/$master" | jq -r .commit.tree.sha)" != "$(mq_git rev-parse "$tip^{tree}")" ]; then
    note "ERROR: $mq_default's tree at $master is not the gated tree at $tip. Not tagged; the queue branch is kept (EVAL-RC-Q.md, \"Recovery\")."
    exit 1
  fi
  gh api "repos/$repo/git/refs" -f ref="refs/tags/$tag" -f sha="$master" >/dev/null
  gh pr comment "$mq_pr_url" --body "**Promoted as $tag:** $(for line in "${members[@]}"; do read -r _ n _ <<<"$line"; printf '#%s ' "$n"; done)squash-merged into $mq_default; its tree is the gated tree." >/dev/null
  gh pr close "$mq_pr_url" >/dev/null
  mq_delete_branch
  note "Promoted ${#members[@]} PR(s) as $tag: $mq_default is at $master, the gated tree. $mq_branch is deleted."
}

# ── Gate ─────────────────────────────────────────────────────────────────────────────────────────────────────────
gate() {
  local due="${1:-}" tip attempt rc n regressed run files
  mq_batch_pr "$repo"
  if [ -z "$mq_pr_url" ]; then
    [ -n "$due" ] && { echo "Nothing to gate: no queue."; exit 0; }
    echo "ERROR: no open batch PR for $mq_branch: nothing to gate." >&2
    exit 1
  fi
  load_members
  if [ -n "$due" ] && ! { [ "$mq_state" = filling ] && [ "${#members[@]}" -gt 0 ]; } && [ "$mq_state" != gating ]; then
    echo "Nothing due: the queue is $mq_state with ${#members[@]} PR(s)."
    exit 0
  fi
  [ "${#members[@]}" -gt 0 ] || { echo "ERROR: the queue holds no PR: nothing to gate." >&2; exit 1; }
  mq_set_state "$repo" gating
  note "Gating $mq_branch: nothing is queued until it ends. Batch PR: $mq_pr_url"
  while :; do
    load_members
    check_members
    if [ "$changed" -eq 1 ] || ! mq_base_current; then rebuild; continue; fi
    tip="$(mq_tip)"
    note "Batch: ${#members[@]} PR(s) at ${tip:0:7}. Running the tests."
    if ! run_tests "$tip"; then
      first_bad tests_ok tests
      drop "$(member_pr "$bad")" "the tests fail with it (the first PR in the queue at which they fail)."
      continue
    fi
    files="$(gated_files "$tip")"
    if [ -n "$files" ]; then
      attempt="$(attempt_of "$tip")" rc=0
      if [ "$attempt" -gt 1 ]; then "$run_evals" --attempt "$attempt" "$mq_pr_url" || rc=$?
      else "$run_evals" "$mq_pr_url" || rc=$?; fi
      if [ "$rc" -eq 75 ]; then
        note "Gate paused before the evals finished (exit 75: another run or the quota). The label stays eval-rc:gating;"
        note "mesh eval-rc-q gate (or tonight's gate --due) resumes it on the same head ${tip:0:7}."
        exit 75
      fi
      case "$(evals_state "$tip")" in
        success) ;;
        failure)
          run="gate-pr$mq_pr_number-${tip:0:7}"; [ "$attempt" -eq 1 ] || run="$run-a$attempt"
          regressed="$(cat "$runs_dir/$run/regressed.json" 2>/dev/null || echo '[]')"
          if [ "$(jq length <<<"$regressed")" -eq 0 ]; then
            gh pr comment "$mq_pr_url" --body "**mesh eval-rc-q gate:** the full evals failed on \`${tip:0:7}\` but no case lost runs (a judge-mean drop?), so no PR can be bisected out. Needs a person (EVAL-RC-Q.md, \"Recovery\")." >/dev/null
            note "Gate stopped: the evals failed but no case regressed, so nothing to bisect. The label stays eval-rc:gating (EVAL-RC-Q.md, \"Recovery\")."
            exit 1
          fi
          mkdir -p "$state_dir/attempts"
          cases_file="$state_dir/cases-${tip:0:12}.json"
          jq '{cases: .}' <<<"$regressed" > "$cases_file"
          note "The evals failed; cases that lost runs: $(jq -r 'join(", ")' <<<"$regressed")."
          if [ "$attempt" -eq 1 ] && cases_ok "$tip" fluke; then
            echo 2 > "$(attempt_file "$tip")"
            gh pr comment "$mq_pr_url" --body "**mesh eval-rc-q gate:** the failing cases ($(jq -r 'join(", ")' <<<"$regressed")) passed when re-run on \`${tip:0:7}\`: a fluke. Gating the batch again." >/dev/null
            note "Fluke: the failing cases pass on a re-run. Gating again (attempt 2)."
            continue
          fi
          first_bad cases_ok bisect
          drop "$(member_pr "$bad")" "the full evals fail with it on $(jq -r 'join(", ")' <<<"$regressed") (re-run and bisected: the first PR in the queue at which they fail)."
          continue ;;
        *) note "Gate stopped without a verdict (evals: $(evals_state "$tip")). The label stays eval-rc:gating: rerun mesh eval-rc-q gate to resume."
           exit 1 ;;
      esac
    else
      note "No queued PR changes an agent file: no evals needed."
    fi
    promote
    [ "$promoted" -eq 1 ] || continue
    exit 0
  done
}

status() {
  local line c n h s f
  mq_batch_pr "$repo"
  mq_fetch
  if ! mq_queue_exists; then
    echo "No queue: $mq_branch does not exist. The next mesh merge-pr creates it from $mq_default."
  else
    mapfile -t members < <(mq_members)
    echo "Queue $mq_branch: ${mq_state:-no batch PR} · ${#members[@]}/$mq_batch PRs · $(mq_base_current && echo "on $mq_default's tip" || echo "$mq_default moved: the gate rebuilds it")"
    echo "Batch PR: ${mq_pr_url:-none}"
    for line in "${members[@]}"; do read -r c n h s <<<"$line"; echo "  #$n at ${h:0:7}: $s"; done
  fi
  for f in "$state_dir"/drops/*.md; do
    [ -e "$f" ] || continue
    echo "Jira note to post for a dropped PR: $f ($(head -n 1 "$f"))"
  done
}

standalone_promote() {
  mq_batch_pr "$repo"
  [ -n "$mq_pr_url" ] || { echo "ERROR: no open batch PR for $mq_branch" >&2; exit 1; }
  load_members
  check_members
  if [ "$changed" -eq 1 ] || ! mq_base_current; then echo "Not promoted: the batch changed; run mesh eval-rc-q gate." >&2; exit 1; fi
  local files
  files="$(gated_files "$(mq_tip)")"
  if [ -n "$files" ] && [ "$(evals_state "$(mq_tip)")" != success ]; then
    echo "Not promoted: the evals have not passed on the batch at $(mq_tip). Run mesh eval-rc-q gate." >&2
    exit 1
  fi
  promote
  [ "$promoted" -eq 1 ] || { echo "Not finished: the batch changed during the promotion; run mesh eval-rc-q gate." >&2; exit 1; }
}

case "${1:-}" in
  -h|--help) help_text; exit 0 ;;
  status) [ $# -eq 1 ] || usage; status ;;
  promote) [ $# -eq 1 ] || usage; standalone_promote ;;
  gate)
    if [ $# -eq 1 ]; then gate
    elif [ $# -eq 2 ] && [ "$2" = --due ]; then gate due
    else usage; fi ;;
  *) usage ;;
esac
