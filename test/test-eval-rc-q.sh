#!/usr/bin/env bash
# alpha-mesh/test/test-eval-rc-q.sh
# The batch queue (PMAT-358, EVAL-RC-Q.md), through `mesh merge-pr` and `mesh eval-rc-q` against a stand-in `gh` that
# keeps the queue's state (the branch eval-rc-q/current, its draft promotion PR #90 and its label, the PRs' bases,
# statuses and tags) in files, and stand-ins for the tests and `mesh run-evals`. Each case is one of the ticket's
# acceptance tests (AT1-AT7).

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export S="$WORK/state" STUB_LOG="$WORK/log"
export MESH_MERGE_QUEUE_CONFIG="$WORK/merge-queue.json"
export MESH_RUN_EVALS="$WORK/run-evals" MESH_EVAL_RC_Q_TESTS="$WORK/tests" MESH_EVAL_RC_Q="$WORK/eval-rc-q"
export MESH_MERGE_POLL_SECONDS=0
printf '{"queues": {"q/r": {"branch": "eval-rc-q/current", "default": "master", "batch_size": 4}}}\n' > "$MESH_MERGE_QUEUE_CONFIG"
TODAY="$(date -u +%F)"
AGENT_FILE="agents/review-pull-request/src/review_pull_request/stage2.py"
PROMO="https://github.com/q/r/pull/90"

mkdir -p "$WORK/bin"
cat > "$WORK/bin/gh" <<'EOF'
#!/usr/bin/env bash
# The stand-in gh. State: $S/master (sha), $S/queue/ (the branch: commits "<parents>|<subject>", files, behind),
# $S/promo/ (the open promotion PR #90: label), $S/pr/<n>/ (base, head, headref, files, mergeable, state),
# $S/status/<sha> ("<context> <state>" lines), $S/tags, $S/comments.
echo "gh $*" >> "$STUB_LOG"
qhead() { printf 'q%039d' "$(wc -l < "$S/queue/commits")"; }
json_status() {  # GitHub's combined status: the latest state per context
  local f="$S/status/$1"; [ -f "$f" ] || { echo '{"statuses":[]}'; return; }
  tac "$f" | awk '!seen[$1]++' | awk 'BEGIN{printf "{\"statuses\":["} {printf "%s{\"context\":\"%s\",\"state\":\"%s\"}", (NR>1?",":""), $1, $2} END{print "]}"}'; }
pr_n() { echo "${1##*/}"; }
case "$1 $2" in
  "pr view")
    n="$(pr_n "$3")"
    if [ "$n" = 90 ]; then
      state=OPEN; [ -d "$S/promo" ] || state=MERGED
      head="$([ -d "$S/queue" ] && qhead || cat "$S/promo-merged-head" 2>/dev/null)"
      printf '{"state":"%s","headRefOid":"%s","baseRefName":"master","headRefName":"eval-rc-q/current","mergeCommit":{"oid":"%s"}}\n' \
        "$state" "$head" "$(cat "$S/master")"
    else
      d="$S/pr/$n"
      printf '{"state":"%s","headRefOid":"%s","baseRefName":"%s","headRefName":"%s","mergeable":"%s","title":"%s"}\n' \
        "$(cat "$d/state")" "$(cat "$d/head")" "$(cat "$d/base")" "$(cat "$d/headref")" "$(cat "$d/mergeable")" "$(cat "$d/title")"
    fi ;;
  "pr list")
    if [[ "$*" == *"--head eval-rc-q/current"* ]]; then
      if [ -d "$S/promo" ]; then
        printf '[{"number":90,"url":"https://github.com/q/r/pull/90","labels":[{"name":"bug"},{"name":"eval-rc:%s"}]}]\n' "$(cat "$S/promo/label")"
      else echo '[]'; fi
    else  # --base eval-rc-q/current: open PRs targeting the queue
      sep=""; printf '['
      for d in "$S"/pr/*/; do
        [ "$(cat "$d/base")" = eval-rc-q/current ] && [ "$(cat "$d/state")" = OPEN ] || continue
        printf '%s{"url":"https://github.com/q/r/pull/%s"}' "$sep" "$(basename "$d")"; sep=,
      done; echo ']'
    fi ;;
  "pr edit")
    n="$(pr_n "$3")"
    if [ "$4" = --base ]; then echo "$5" > "$S/pr/$n/base"
    else
      shift 3
      while [ $# -gt 0 ]; do [ "$1" = --add-label ] && echo "${2#eval-rc:}" > "$S/promo/label"; shift 2; done
    fi ;;
  "pr merge")
    n="$(pr_n "$3")"
    if [ "$n" = 90 ]; then  # pr merge <promo> --merge --match-head-commit <sha> --subject <s> --body <b>
      [ "$4" = --merge ] && [ "$6" = "$(qhead)" ] || { echo "stub: wrong promotion merge: $*" >&2; exit 1; }
      qhead > "$S/promo-merged-head"
      echo "p$(qhead | cut -c2-)" > "$S/master"
      echo "tree-$(qhead)" > "$S/master-tree"; [ -z "${STUB_TREE_MISMATCH:-}" ] || echo other > "$S/master-tree"
      rm -rf "$S/promo"
    else  # pr merge <url> --squash --match-head-commit <sha>
      [ "$(cat "$S/pr/$n/base")" = eval-rc-q/current ] || [[ "$(cat "$S/pr/$n/headref")" == hotfix/* ]] || [ -n "${STUB_NO_QUEUE:-}" ] \
        || { echo "stub: merged into $(cat "$S/pr/$n/base")" >&2; exit 1; }
      if [ "$(cat "$S/pr/$n/base")" = eval-rc-q/current ]; then
        echo "1|$(cat "$S/pr/$n/title") (#$n)" >> "$S/queue/commits"
        cat "$S/pr/$n/files" >> "$S/queue/files"
      else
        echo "h$n" > "$S/master"; echo 1 > "$S/master-moved"
        [ -d "$S/queue" ] && echo 1 > "$S/queue/behind"
      fi
      echo MERGED > "$S/pr/$n/state"
    fi ;;
  "pr create")
    mkdir -p "$S/promo"; echo filling > "$S/promo/label"
    [[ "$*" == *"--draft"* && "$*" == *"--label eval-rc:filling"* && "$*" == *"--head eval-rc-q/current"* ]] \
      || { echo "stub: promotion PR created without --draft, its label or the queue head: $*" >&2; exit 1; }
    echo "https://github.com/q/r/pull/90" ;;
  "pr ready"|"pr checks") ;;
  "pr comment") echo "$*" >> "$S/comments" ;;
  "pr diff")
    n="$(pr_n "$3")"
    if [ "$n" = 90 ]; then cat "$S/queue/files" 2>/dev/null; else cat "$S/pr/$n/files"; fi ;;
  "api -X")
    if [ "$3" = DELETE ]; then rm -rf "$S/queue"
    else  # api -X POST repos/q/r/merges -f base=... -f head=master -f commit_message=...
      [ -z "${STUB_MERGE_CONFLICT:-}" ] || { echo "gh: Merge conflict (HTTP 409)" >&2; exit 1; }
      echo "2|${10#commit_message=}" >> "$S/queue/commits"; echo 0 > "$S/queue/behind"
    fi ;;
  "api repos/q/r/branches/eval-rc-q/current") [ -d "$S/queue" ] || { echo "gh: Branch not found (HTTP 404)" >&2; exit 1; }; echo '{}' ;;
  "api repos/q/r/compare/master...eval-rc-q/current")
    printf '{"behind_by":%s,"commits":[' "$(cat "$S/queue/behind")"
    awk -F'|' '{printf "%s{\"parents\":[%s],\"commit\":{\"message\":\"%s\\nbody\"}}", (NR>1?",":""), ($1==2?"{},{}":"{}"), $2}' "$S/queue/commits"
    echo ']}' ;;
  "api repos/q/r/git/ref/heads/master") printf '{"object":{"sha":"%s"}}\n' "$(cat "$S/master")" ;;
  "api repos/q/r/git/refs")  # api repos/q/r/git/refs -f ref=refs/<...> -f sha=<sha>
    ref="${4#ref=}"
    if [ "$ref" = refs/heads/eval-rc-q/current ]; then
      [ "${6#sha=}" = "$(cat "$S/master")" ] || { echo "stub: queue not created from master" >&2; exit 1; }
      mkdir -p "$S/queue"; : > "$S/queue/commits"; : > "$S/queue/files"; echo 0 > "$S/queue/behind"
    else echo "${ref#refs/tags/} ${6#sha=}" >> "$S/tags"; fi ;;
  *)
    case "$2" in
      repos/q/r/git/matching-refs/tags/*)
        prefix="${2#repos/q/r/git/matching-refs/tags/}"; sep=""; printf '['
        grep "^$prefix" "$S/tags" 2>/dev/null | while read -r t _; do printf '%s{"ref":"refs/tags/%s"}' "$sep" "$t"; sep=,; done
        echo ']' ;;
      repos/q/r/commits/*/status) json_status "$(cut -d/ -f5 <<<"$2")" ;;
      repos/q/r/commits/*)
        sha="${2##*/}"
        if [ "$sha" = "$(cat "$S/master")" ]; then t="$(cat "$S/master-tree")"; else t="tree-$sha"; fi
        printf '{"commit":{"tree":{"sha":"%s"}}}\n' "$t" ;;
      repos/q/r/pulls/*/commits*)
        n="$(cut -d/ -f5 <<<"$2")"
        if [ "$n" = 90 ]; then printf '[{"sha":"%s"}]\n' "$(qhead)"; else printf '[{"sha":"%s"}]\n' "$(cat "$S/pr/$n/head")"; fi ;;
      repos/q/r/compare/master...*) printf '{"behind_by":%s}\n' "$(cat "$S/pr/behind" 2>/dev/null || echo 0)" ;;
      *) echo "unexpected gh call: $*" >&2; exit 9 ;;
    esac ;;
esac
EOF
# The stand-in mesh run-evals: posts evals (success or failure) on the promotion PR's head, or stops (75: quota).
cat > "$WORK/run-evals" <<'EOF'
#!/usr/bin/env bash
echo "run-evals $*" >> "$STUB_LOG"
head="$(printf 'q%039d' "$(wc -l < "$S/queue/commits")")"
case "${STUB_EVALS:-success}" in
  quota) echo "Not run: 12 free-model requests left today" >&2; exit 75 ;;
  *) r="${STUB_EVALS:-success}"; mkdir -p "$S/status"; echo "evals $r" >> "$S/status/$head"; [ "$r" = success ] ;;
esac
EOF
cat > "$WORK/tests" <<'EOF'
#!/usr/bin/env bash
echo "tests $*" >> "$STUB_LOG"
[ "${STUB_TESTS:-pass}" = pass ]
EOF
cat > "$WORK/eval-rc-q" <<'EOF'
#!/usr/bin/env bash
echo "eval-rc-q $*" >> "$STUB_LOG"
EOF
chmod +x "$WORK/bin/gh" "$WORK/run-evals" "$WORK/tests" "$WORK/eval-rc-q"

fresh() {  # a master with no queue, no PRs
  rm -rf "$S"; mkdir -p "$S/pr" "$S/status"; echo "m$(printf '%039d' 0)" > "$S/master"; : > "$S/tags"
}
add_pr() {  # add_pr <n> <files> [headref] [mergeable]
  local d="$S/pr/$1"; mkdir -p "$d"
  echo OPEN > "$d/state"; echo master > "$d/base"; printf 'h%039d' "$1" > "$d/head"
  echo "${3:-feature/PMAT-$1-x}" > "$d/headref"; echo "${4:-MERGEABLE}" > "$d/mergeable"
  echo "feature[PMAT-$1]: did thing $1" > "$d/title"; printf '%s\n' "$2" > "$d/files"
}
smoke_ok() { printf 'evals-smoke success\n' >> "$S/status/$(cat "$S/pr/$1/head")"; }
mesh() {  # mesh <args...>: sets $out and $code; logs to $STUB_LOG
  : > "$STUB_LOG"
  set +e
  out="$(PATH="$WORK/bin:$PATH" MESH_ASSUME_TTY="${TTY:-}" "$MESH_DIR/bin/mesh" "$@" 2>&1 <<<"${ANSWER:-}")"
  code=$?
  set -e
}
fails=0
check() {  # check <name> <condition...>
  local name="$1"; shift
  if "$@"; then echo "  PASS: $name"; else
    echo "  FAIL: $name"; echo "$out" | sed 's/^/    out: /'; sed 's/^/    log: /' "$STUB_LOG"; fails=$((fails + 1)); fi
}
logged() { grep -qF -- "$1" "$STUB_LOG"; }
not_logged() { ! grep -qF -- "$1" "$STUB_LOG"; }
says() { grep -qiF -- "$1" <<<"$out"; }
label() { cat "$S/promo/label" 2>/dev/null; }
ticket() { echo "https://github.com/q/r/pull/$1"; }

echo "=== eval-rc-q tests ==="

# AT1 creating the queue.
fresh; add_pr 11 README.md
mesh merge-pr "$(ticket 11)"
check "AT1: with no queue, merge-pr creates eval-rc-q/current from master, moves the PR there, squash-merges it and opens the draft promotion PR (eval-rc:filling)" \
  bash -c '[ "$0" -eq 0 ] && grep -qF "gh api repos/q/r/git/refs -f ref=refs/heads/eval-rc-q/current -f sha=m" "$1" \
    && grep -qF "gh pr edit https://github.com/q/r/pull/11 --base eval-rc-q/current" "$1" \
    && grep -qF "gh pr merge https://github.com/q/r/pull/11 --squash --match-head-commit h" "$1" \
    && grep -qF "gh pr create -R q/r --draft --base master --head eval-rc-q/current" "$1" \
    && [ "$(cat "$2/promo/label")" = filling ] && [ "$(cat "$2/master")" = "m$(printf %039d 0)" ]' "$code" "$STUB_LOG" "$S"
check "AT1: it reports the queue and the promotion PR" says "$PROMO"

# AT4 an agent PR without smoke is refused; with smoke it merges; a non-agent PR needs none.
add_pr 12 "$AGENT_FILE"
mesh merge-pr "$(ticket 12)"
check "AT4: an agent PR without evals-smoke = success is refused and nothing moves" \
  bash -c '[ "$0" -ne 0 ] && ! grep -q "gh pr merge\|gh pr edit" "$1"' "$code" "$STUB_LOG"
check "AT4: the refusal names the smoke command" says "mesh run-evals --smoke $(ticket 12)"
smoke_ok 12
mesh merge-pr "$(ticket 12)"
check "AT4: with evals-smoke = success the agent PR merges into the queue" \
  bash -c '[ "$0" -eq 0 ] && grep -qF "gh pr merge https://github.com/q/r/pull/12 --squash" "$1" && ! grep -qF "gh pr create" "$1"' "$code" "$STUB_LOG"

# A PR that conflicts with the queue is refused and moved back to master.
add_pr 13 README.md "" CONFLICTING
mesh merge-pr "$(ticket 13)"
check "a PR that conflicts with the queue is refused, unmerged, and its base set back to master" \
  bash -c '[ "$0" -ne 0 ] && ! grep -q "gh pr merge" "$1" && [ "$(cat "$2/pr/13/base")" = master ]' "$code" "$STUB_LOG" "$S"
check "the conflict refusal says what to do" says "conflicts with the queue"

# AT2 a full queue: the 4th PR offers to gate it; a 5th is refused.
add_pr 14 README.md; add_pr 15 docs.md
TTY=1 ANSWER=n mesh merge-pr "$(ticket 14)"   # 3rd
check "the 3rd PR merges without the gate offer" bash -c '[ "$0" -eq 0 ] && ! grep -qi "gate it now" <<<"$1"' "$code" "$out"
echo OPEN > "$S/pr/13/state"; echo MERGEABLE > "$S/pr/13/mergeable"
TTY=1 ANSWER=y mesh merge-pr "$(ticket 13)"   # 4th
check "AT2: the 4th PR prompts 'Queue full: gate it now?' and on yes runs mesh eval-rc-q gate" \
  bash -c '[ "$0" -eq 0 ] && grep -qi "Queue full: gate it now" <<<"$1" && grep -qx "eval-rc-q gate" "$2"' "$code" "$out" "$STUB_LOG"
mesh merge-pr "$(ticket 15)"
check "AT2: a 5th PR is refused while the queue is full" \
  bash -c '[ "$0" -ne 0 ] && ! grep -q "gh pr merge\|gh pr edit" "$1"' "$code" "$STUB_LOG"

# status
mesh eval-rc-q status
check "status prints the state, the batch and the promotion PR" \
  bash -c '[ "$0" -eq 0 ] && grep -q "filling" <<<"$1" && grep -q "4/4" <<<"$1" && grep -qF "PMAT-12" <<<"$1" && grep -qF "$2" <<<"$1"' "$code" "$out" "$PROMO"

# AT3 a blocked queue: gating or fixing refuses every merge.
for st in gating fixing; do
  echo "$st" > "$S/promo/label"; add_pr 16 README.md
  mesh merge-pr "$(ticket 16)"
  check "AT3: with the queue $st, merge-pr refuses, says to clear it first, prints the promotion PR and merges nothing" \
    bash -c '[ "$0" -ne 0 ] && grep -qi "clear it first" <<<"$1" && grep -qF "$2" <<<"$1" && ! grep -q "gh pr merge\|gh pr edit" "$3"' \
    "$code" "$out" "$PROMO" "$STUB_LOG"
done

# AT5 gate and promote: master moved (a hotfix), agent files in the batch; the evals fail, then pass.
echo filling > "$S/promo/label"; echo 1 > "$S/queue/behind"
STUB_EVALS=failure mesh eval-rc-q gate
check "AT5: a failing gate relabels the promotion PR eval-rc:fixing and leaves master unchanged" \
  bash -c '[ "$0" -ne 0 ] && [ "$(cat "$1/promo/label")" = fixing ] && ! grep -q "gh pr merge" "$2" && [ "$(cat "$1/master")" = "m$(printf %039d 0)" ]' \
  "$code" "$S" "$STUB_LOG"
check "AT5: the gate merged master in first, with the commit message format, then ran the tests, then the evals on the promotion PR" \
  bash -c 'grep -qF "gh api -X POST repos/q/r/merges -f base=eval-rc-q/current -f head=master -f commit_message=chore[eval-rc-q-$0-1]: merged master before the gate" "$1" \
    && [ "$(grep -n "^tests" "$1" | cut -d: -f1)" -lt "$(grep -n "^run-evals" "$1" | cut -d: -f1)" ] && grep -qx "run-evals $2" "$1"' \
  "$TODAY" "$STUB_LOG" "$PROMO"
check "AT5: the failure is posted on the promotion PR" bash -c 'grep -q "pr comment https://github.com/q/r/pull/90" "$0/comments"' "$S"
STUB_EVALS=quota mesh eval-rc-q gate
check "a gate stopped by the quota keeps eval-rc:gating, merges nothing and says it resumes" \
  bash -c '[ "$0" -eq 75 ] && [ "$(cat "$1/promo/label")" = gating ] && ! grep -q "gh pr merge" "$2" && grep -qi "resume" <<<"$3"' \
  "$code" "$S" "$STUB_LOG" "$out"
gated="$(printf 'q%039d' "$(wc -l < "$S/queue/commits")")"
STUB_EVALS=success mesh eval-rc-q gate
check "AT5: a passing gate promotes: merge commit at the gated sha, with the promotion subject" \
  bash -c '[ "$0" -eq 0 ] && grep -qF "gh pr ready $1" "$2" \
    && grep -qF "gh pr merge $1 --merge --match-head-commit $3 --subject chore[eval-rc-q-$4-1]: promoted PMAT-11, PMAT-12, PMAT-14, PMAT-13 (#11, #12, #14, #13)" "$2"' \
  "$code" "$PROMO" "$STUB_LOG" "$gated" "$TODAY"
check "AT5: then tags eval-rc-q-<date>-1 on the merge commit and deletes the branch" \
  bash -c 'grep -qx "eval-rc-q-$0-1 $(cat "$1/master")" "$1/tags" && [ ! -d "$1/queue" ] && [ ! -d "$1/promo" ]' "$TODAY" "$S"
check "AT5: after it, master's tree equals the gated tree" bash -c '[ "$(cat "$0/master-tree")" = "tree-$1" ]' "$S" "$gated"

# AT6 after a promotion, the next merge creates a fresh queue from the new master; a second promotion the same day is -2.
add_pr 17 README.md
mesh merge-pr "$(ticket 17)"
check "AT6: after a promotion, the next merge-pr creates a fresh eval-rc-q/current from the new master" \
  bash -c '[ "$0" -eq 0 ] && grep -qF "gh api repos/q/r/git/refs -f ref=refs/heads/eval-rc-q/current -f sha=$(cat "$1/master")" "$2" \
    && [ "$(cat "$1/promo/label")" = filling ]' "$code" "$S" "$STUB_LOG"

# A batch with no agent file passes its gate at once: no evals run.
mesh eval-rc-q gate
check "a batch with no agent file is promoted without running the evals, tagged -2 the same day" \
  bash -c '[ "$0" -eq 0 ] && ! grep -q "^run-evals" "$1" && grep -q "^eval-rc-q-$2-2 " "$3/tags"' "$code" "$STUB_LOG" "$TODAY" "$S"

# Failing tests stop the gate before the evals.
add_pr 18 "$AGENT_FILE"; smoke_ok 18; mesh merge-pr "$(ticket 18)"
STUB_TESTS=fail mesh eval-rc-q gate
check "failing tests relabel eval-rc:fixing and never run the evals" \
  bash -c '[ "$0" -ne 0 ] && [ "$(cat "$1/promo/label")" = fixing ] && ! grep -q "^run-evals" "$2"' "$code" "$S" "$STUB_LOG"

# A conflict merging master into the queue: fixing, nothing else runs.
echo filling > "$S/promo/label"; echo 1 > "$S/queue/behind"
STUB_MERGE_CONFLICT=1 mesh eval-rc-q gate
check "a conflict merging master in relabels eval-rc:fixing and runs neither tests nor evals" \
  bash -c '[ "$0" -ne 0 ] && [ "$(cat "$1/promo/label")" = fixing ] && ! grep -q "^tests\|^run-evals" "$2"' "$code" "$S" "$STUB_LOG"

# gate --due (the nightly cron): only a filling queue holding a PR, or a gate to resume.
echo fixing > "$S/promo/label"; echo 0 > "$S/queue/behind"
mesh eval-rc-q gate --due
check "gate --due leaves a queue being fixed alone" bash -c '[ "$0" -eq 0 ] && ! grep -q "^tests\|gh pr edit" "$1"' "$code" "$STUB_LOG"
echo gating > "$S/promo/label"
mesh eval-rc-q gate --due
check "gate --due resumes a gate left gating (stopped by the quota or a reboot)" \
  bash -c '[ "$0" -eq 0 ] && grep -q "^run-evals" "$1" && [ ! -d "$2/promo" ]' "$code" "$STUB_LOG" "$S"
add_pr 19 README.md; mesh merge-pr "$(ticket 19)"; rm -rf "$S/queue"/commits; : > "$S/queue/commits"
mesh eval-rc-q gate --due
check "gate --due does nothing for a queue with no PR" bash -c '[ "$0" -eq 0 ] && ! grep -q "^tests" "$1"' "$code" "$STUB_LOG"

# The tree check: a promotion whose master tree differs from the gated tree is reported, never silent.
echo "1|feature[PMAT-19]: did thing 19 (#19)" > "$S/queue/commits"
STUB_TREE_MISMATCH=1 mesh eval-rc-q gate
check "a promotion whose master tree is not the gated tree fails loudly" bash -c '[ "$0" -ne 0 ] && grep -qi "tree" <<<"$1"' "$code" "$out"

# AT7 a hotfix merges straight into master, under today's rules; the next gate merges it into the queue first.
fresh; add_pr 21 README.md; mesh merge-pr "$(ticket 21)"
add_pr 22 README.md hotfix/PMAT-22-broken-master
mesh merge-pr "$(ticket 22)"
check "AT7: a hotfix/* PR squash-merges straight into master, not the queue" \
  bash -c '[ "$0" -eq 0 ] && grep -qF "gh pr merge https://github.com/q/r/pull/22 --squash --match-head-commit" "$1" \
    && [ "$(cat "$2/pr/22/base")" = master ] && ! grep -q "gh pr edit" "$1"' "$code" "$STUB_LOG" "$S"
mesh eval-rc-q gate
check "AT7: the next gate merges master (with the hotfix) into the queue first" \
  bash -c '[ "$0" -eq 0 ] && grep -qF "gh api -X POST repos/q/r/merges -f base=eval-rc-q/current -f head=master" "$1"' "$code" "$STUB_LOG"
add_pr 23 "$AGENT_FILE" hotfix/PMAT-23-agent
mesh merge-pr "$(ticket 23)"
check "AT7: an agent hotfix still needs the full evals status, as today" \
  bash -c '[ "$0" -ne 0 ] && ! grep -q "gh pr merge" "$1" && grep -qF "mesh run-evals https://github.com/q/r/pull/23" <<<"$2"' "$code" "$STUB_LOG" "$out"

# A repo with no queue merges straight into its base, as before.
printf '{"queues": {}}\n' > "$MESH_MERGE_QUEUE_CONFIG"
fresh; add_pr 31 README.md; STUB_NO_QUEUE=1 mesh merge-pr "$(ticket 31)"
check "a repo with no queue in config/merge-queue.json merges into its base as before" \
  bash -c '[ "$0" -eq 0 ] && grep -qF "gh pr merge https://github.com/q/r/pull/31 --squash" "$1" && ! grep -q "eval-rc-q" "$1"' "$code" "$STUB_LOG"

[ "$fails" -eq 0 ] || exit 1
