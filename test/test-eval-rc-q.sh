#!/usr/bin/env bash
# alpha-mesh/test/test-eval-rc-q.sh
# The batch queue (PMAT-358 v2, EVAL-RC-Q.md), through `mesh merge-pr` and `mesh eval-rc-q`. Git is real: a bare repo
# is `origin`, so the queue's commits, rebuilds, branch updates and squash merges are real commits and trees. A
# stand-in `gh` plays GitHub around it (PRs, labels, statuses, tags) and enforces the master ruleset (a branch up to
# date, and `eval-rc-q` = success, or `evals` for an agent hotfix). Stand-ins for the tests and `mesh run-evals` fail
# on marker files in the tree under test: BAD (the evals fail, reproducibly), FLAKY (the first full run fails, a
# re-run passes), TESTFAIL (the tests fail). Each case is one of the ticket's v2 acceptance tests.

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
export S="$WORK/state" STUB_LOG="$WORK/log" ORIGIN="$WORK/origin.git"
export MESH_MERGE_QUEUE_CONFIG="$WORK/merge-queue.json" MESH_EVAL_RC_Q_REPO_DIR="$WORK/repo"
export MESH_RUN_EVALS="$WORK/run-evals" MESH_EVAL_RC_Q_TESTS="$WORK/tests" MESH_EVAL_RC_Q="$WORK/eval-rc-q"
export MESH_EVAL_RUNS="$WORK/runs" MESH_EVAL_RC_Q_STATE="$WORK/qstate" MESH_MERGE_POLL_SECONDS=0
export GIT_AUTHOR_NAME=t GIT_AUTHOR_EMAIL=t@t GIT_COMMITTER_NAME=t GIT_COMMITTER_EMAIL=t@t
queue_config() { printf '{"queues": {"q/r": {"branch": "eval-rc-q/current", "default": "master", "batch_size": 4, "jira": "PMAT-358"}}}\n' > "$MESH_MERGE_QUEUE_CONFIG"; }
queue_config
TODAY="$(date -u +%F)"
AGENT="agents/review-pull-request/src/review_pull_request"
BATCH="https://github.com/q/r/pull/90"
og() { git --git-dir="$ORIGIN" "$@"; }

mkdir -p "$WORK/bin"
cat > "$WORK/bin/gh" <<'EOF'
#!/usr/bin/env bash
# The stand-in gh over the bare repo $ORIGIN. State in $S: pr/<n>/{state,title,headref}, batch/label (the open batch
# PR #90), status/<sha> ("<context> <state>" lines), comments.
echo "gh $*" >> "$STUB_LOG"
og() { git --git-dir="$ORIGIN" "$@"; }
n_of() { echo "${1##*/}"; }
head_of() { og rev-parse "refs/pull/$1/head"; }
status_json() {  # GitHub's combined status: the latest state per context
  local f="$S/status/$1"; [ -f "$f" ] || { echo '{"statuses":[]}'; return; }
  tac "$f" | awk '!seen[$1]++' | jq -Rn '{statuses: [inputs | split(" ") | {context: .[0], state: .[1]}]}'
}
ctx() { status_json "$1" | jq -r --arg c "$2" '[.statuses[] | select(.context == $c)][0].state // "missing"'; }
agent_files() { og diff --name-only "$(og merge-base master "$1")" "$1" | grep -q '^agents/review-pull-request/src/'; }
hotfix_master() {  # a commit straight on master, as a hotfix would land
  local blob tree
  blob="$(echo "$1" | og hash-object -w --stdin)"
  tree="$( { og ls-tree master; printf '100644 blob %s\t%s\n' "$blob" "$1"; } | og mktree)"
  og update-ref refs/heads/master "$(echo "hotfix $1" | og commit-tree "$tree" -p "$(og rev-parse master)")"
}
case "$1 $2" in
  "pr view")
    n="$(n_of "$3")"
    if [ "$n" = 90 ]; then
      st=OPEN; [ -d "$S/batch" ] || st=CLOSED
      jq -n --arg s "$st" --arg h "$(og rev-parse -q --verify refs/heads/eval-rc-q/current || true)" \
        '{state: $s, headRefOid: $h, baseRefName: "master", headRefName: "eval-rc-q/current", title: "batch"}'
    else
      d="$S/pr/$n"
      jq -n --arg s "$(cat "$d/state")" --arg h "$(head_of "$n")" --arg r "$(cat "$d/headref")" --arg t "$(cat "$d/title")" \
        '{state: $s, headRefOid: $h, baseRefName: "master", headRefName: $r, title: $t}'
    fi ;;
  "pr list")
    if [ -d "$S/batch" ]; then
      jq -n --arg l "$(cat "$S/batch/label")" '[{number: 90, url: "https://github.com/q/r/pull/90", labels: [{name: "bug"}, {name: ("eval-rc:" + $l)}]}]'
    else echo '[]'; fi ;;
  "pr create")
    [[ "$*" == *"--draft"* && "$*" == *"--label eval-rc:filling"* ]] || { echo "stub: batch PR not a labelled draft" >&2; exit 1; }
    og rev-parse -q --verify refs/heads/eval-rc-q/current >/dev/null || { echo "stub: no queue branch to open a PR from" >&2; exit 1; }
    mkdir -p "$S/batch"; echo filling > "$S/batch/label"; echo "https://github.com/q/r/pull/90" ;;
  "pr edit")  # pr edit <batch> --add-label eval-rc:X --remove-label ...
    shift 3; while [ $# -gt 0 ]; do [ "$1" = --add-label ] && echo "${2#eval-rc:}" > "$S/batch/label"; shift 2; done ;;
  "pr close") rm -rf "$S/batch" ;;
  "pr comment") echo "$3 $5" >> "$S/comments" ;;
  "pr ready"|"pr checks") ;;
  "pr diff")
    n="$(n_of "$3")"
    if [ "$n" = 90 ]; then og diff --name-only master eval-rc-q/current; else og diff --name-only "$(og merge-base master "$(head_of "$n")")" "$(head_of "$n")"; fi ;;
  "pr merge")  # pr merge <url> --squash --match-head-commit <sha>: the master ruleset, then a real squash commit
    n="$(n_of "$3")"; h="$(head_of "$n")"
    if [ "${STUB_MERGE_FAIL_ONCE:-}" = "$n" ] && [ ! -f "$S/merge-failed" ]; then
      touch "$S/merge-failed"; echo "stub: the merge was killed" >&2; exit 1
    fi
    [ "$h" = "$6" ] || { echo "stub: head moved" >&2; exit 1; }
    [ "$(og rev-list --count "$h..master")" = 0 ] || { echo "stub ruleset: branch not up to date with master" >&2; exit 1; }
    if [[ "$(cat "$S/pr/$n/headref")" != hotfix/* ]] && [ -z "${STUB_NO_QUEUE:-}" ]; then
      [ "$(ctx "$h" eval-rc-q)" = success ] || { echo "stub ruleset: evals-gate fails: no eval-rc-q = success" >&2; exit 1; }
    fi
    if agent_files "$h"; then [ "$(ctx "$h" evals)" = success ] || { echo "stub ruleset: agent PR without evals" >&2; exit 1; }; fi
    c="$(echo "$(cat "$S/pr/$n/title") (#$n)" | og commit-tree "$h^{tree}" -p "$(og rev-parse master)")"
    og update-ref refs/heads/master "$c"; echo MERGED > "$S/pr/$n/state"
    [ "${STUB_HOTFIX_AFTER_MERGE:-}" != "$n" ] || hotfix_master hot-after-merge.txt ;;
  "run list")  # the newest evals-gate run on --commit <sha>: passed, unless STUB_CHECK_FAIL_FIRST and not re-run yet
    if [ -n "${STUB_CHECK_FAIL_FIRST:-}" ] && [ ! -f "$S/rerun" ]; then
      echo '[{"databaseId": 7, "status": "completed", "conclusion": "failure"}]'
    else echo '[{"databaseId": 7, "status": "completed", "conclusion": "success"}]'; fi ;;
  "run rerun") touch "$S/rerun" ;;
  "api -X")
    if [ "$3" = PUT ]; then  # api -X PUT repos/q/r/pulls/<n>/update-branch -f expected_head_sha=<sha>: merge master in
      n="$(cut -d/ -f5 <<<"$4")"; h="$(head_of "$n")"
      [ "${6#expected_head_sha=}" = "$h" ] || { echo "stub: expected head" >&2; exit 1; }
      tree="$(og merge-tree --write-tree "$h" master | head -n 1)" || { echo "stub: update-branch conflict" >&2; exit 1; }
      if [ "${STUB_UPDATE_EXTRA:-}" = "$n" ]; then  # GitHub's merge differs from what was gated
        blob="$(echo extra | og hash-object -w --stdin)"
        tree="$( { og ls-tree "$tree"; printf '100644 blob %s\tEXTRA\n' "$blob"; } | og mktree)"
      fi
      m="$(echo "Merge master into pr $n" | og commit-tree "$tree" -p "$h" -p "$(og rev-parse master)")"
      og update-ref "refs/pull/$n/head" "$m"; og update-ref "refs/heads/$(cat "$S/pr/$n/headref")" "$m"
    else echo "unexpected gh -X: $*" >&2; exit 9; fi ;;
  "api repos/q/r/git/ref/heads/master") jq -n --arg s "$(og rev-parse master)" '{object: {sha: $s}}' ;;
  "api repos/q/r/git/refs")  # api repos/q/r/git/refs -f ref=refs/tags/<t> -f sha=<sha>
    og update-ref "${4#ref=}" "${6#sha=}" ;;
  *)
    case "$2" in
      repos/q/r/git/matching-refs/tags/*)
        og for-each-ref --format='%(refname)' "refs/tags/${2#repos/q/r/git/matching-refs/tags/}*" | jq -Rn '[inputs | {ref: .}]' ;;
      repos/q/r/commits/*/status) status_json "$(cut -d/ -f5 <<<"$2")" ;;
      repos/q/r/commits/*) jq -n --arg t "$(og rev-parse "${2##*/}^{tree}")" '{commit: {tree: {sha: $t}}}' ;;
      repos/q/r/statuses/*)  # api repos/q/r/statuses/<sha> -f state=<s> -f context=<c> -f description=<d>
        mkdir -p "$S/status"; echo "${6#context=} ${4#state=}" >> "$S/status/${2##*/}" ;;
      repos/q/r/pulls/*/commits*) jq -n --arg h "$(head_of "$(cut -d/ -f5 <<<"$2")")" '[{sha: $h}]' ;;
      repos/q/r/compare/master...*) jq -n --argjson b "$(og rev-list --count "${2##*...}..master")" '{behind_by: $b}' ;;
      *) echo "unexpected gh call: $*" >&2; exit 9 ;;
    esac ;;
esac
EOF
# The stand-in mesh run-evals. Full: on the batch PR's head (BAD: fail; FLAKY: fail on attempt 1), posting evals and
# writing regressed.json. --cases: at a commit (BAD: fail; FLAKY passes, it was a fluke). STUB_QUOTA_ONCE: exit 75 once.
cat > "$WORK/run-evals" <<'EOF'
#!/usr/bin/env bash
echo "run-evals $*" >> "$STUB_LOG"
og() { git --git-dir="$ORIGIN" "$@"; }
has() { og cat-file -e "$1:$2" 2>/dev/null; }
if [ -n "${STUB_QUOTA_ONCE:-}" ] && [ ! -f "$S/quota-spent" ]; then touch "$S/quota-spent"; echo "Not run: quota" >&2; exit 75; fi
if [ "$1" = --cases ]; then
  [ -z "${STUB_DRIFT:-}" ] || exit 1       # drift: the cases fail everywhere, master too
  ! has "$4" BAD; exit
fi
attempt=1; [ "$1" = --attempt ] && { attempt="$2"; shift 2; }
h="$(og rev-parse eval-rc-q/current)"; run="gate-pr90-${h:0:7}"; [ "$attempt" = 1 ] || run="$run-a$attempt"
r=success
if has "$h" BAD || { has "$h" FLAKY && [ "$attempt" = 1 ]; } || [ -n "${STUB_DRIFT:-}${STUB_MEANS:-}" ]; then r=failure; fi
if [ -n "${STUB_HOTFIX_DURING_EVALS:-}" ] && [ ! -f "$S/hotfixed" ]; then  # a hotfix lands while the evals run
  touch "$S/hotfixed"
  blob="$(echo hot | og hash-object -w --stdin)"
  tree="$( { og ls-tree master; printf '100644 blob %s\thot-during-evals.txt\n' "$blob"; } | og mktree)"
  og update-ref refs/heads/master "$(echo "hotfix during evals" | og commit-tree "$tree" -p "$(og rev-parse master)")"
fi
mkdir -p "$MESH_EVAL_RUNS/$run" "$S/status"
if [ "$r" = success ] || [ -n "${STUB_MEANS:-}" ]; then echo '[]' > "$MESH_EVAL_RUNS/$run/regressed.json"
else echo '["sec-001", "log-002"]' > "$MESH_EVAL_RUNS/$run/regressed.json"; fi
echo "evals $r" >> "$S/status/$h"
[ "$r" = success ]
EOF
cat > "$WORK/tests" <<'EOF'
#!/usr/bin/env bash
echo "tests $*" >> "$STUB_LOG"
! git --git-dir="$ORIGIN" cat-file -e "$1:TESTFAIL" 2>/dev/null
EOF
cat > "$WORK/eval-rc-q" <<'EOF'
#!/usr/bin/env bash
echo "eval-rc-q $*" >> "$STUB_LOG"
EOF
chmod +x "$WORK/bin/gh" "$WORK/run-evals" "$WORK/tests" "$WORK/eval-rc-q"

fresh() {  # an origin with master (README.md), a clone of it as the repo dir, no PRs, no queue
  rm -rf "$S" "$ORIGIN" "$WORK/repo" "$WORK/seed" "$MESH_EVAL_RUNS" "$MESH_EVAL_RC_Q_STATE"
  mkdir -p "$S/pr" "$S/status"
  git init -q --bare -b master "$ORIGIN"
  git init -q -b master "$WORK/seed"; echo base > "$WORK/seed/README.md"
  git -C "$WORK/seed" add -A; git -C "$WORK/seed" commit -qm base; git -C "$WORK/seed" push -q "$ORIGIN" master
  git clone -q "$ORIGIN" "$WORK/repo"
}
add_pr() {  # add_pr <n> <path> [content] [headref]: a branch from master changing <path>, and its PR
  local n="$1" d="$S/pr/$1" ref="${4:-feature/PMAT-$1-x}"
  git -C "$WORK/seed" fetch -q "$ORIGIN" master; git -C "$WORK/seed" checkout -q -B "$ref" FETCH_HEAD
  mkdir -p "$WORK/seed/$(dirname "$2")"; echo "${3:-change $n}" > "$WORK/seed/$2"
  git -C "$WORK/seed" add -A; git -C "$WORK/seed" commit -qm "work $n"
  git -C "$WORK/seed" push -q "$ORIGIN" "HEAD:refs/heads/$ref" "HEAD:refs/pull/$n/head"
  mkdir -p "$d"; echo OPEN > "$d/state"; echo "$ref" > "$d/headref"; echo "feature[PMAT-$n]: did thing $n" > "$d/title"
}
push_pr() {  # push_pr <n>: one more commit on PR <n>
  git -C "$WORK/seed" fetch -q "$ORIGIN" "refs/pull/$1/head"; git -C "$WORK/seed" checkout -q -B tmp FETCH_HEAD
  echo more >> "$WORK/seed/more-$1"; git -C "$WORK/seed" add -A; git -C "$WORK/seed" commit -qm more
  git -C "$WORK/seed" push -q -f "$ORIGIN" "HEAD:refs/pull/$1/head" "HEAD:refs/heads/$(cat "$S/pr/$1/headref")"
}
smoke_ok() { echo "evals-smoke success" >> "$S/status/$(og rev-parse "refs/pull/$1/head")"; }
url() { echo "https://github.com/q/r/pull/$1"; }
mesh() {  # mesh <args...>: sets $out and $code
  : > "$STUB_LOG"
  set +e
  out="$(PATH="$WORK/bin:$PATH" MESH_ASSUME_TTY="${TTY:-}" "$MESH_DIR/bin/mesh" "$@" 2>&1 <<<"${ANSWER:-}")"
  code=$?
  set -e
}
queue_all() { local n; for n in "$@"; do mesh merge-pr "$(url "$n")"; [ "$code" -eq 0 ] || { echo "setup: queue #$n failed: $out" >&2; exit 1; }; done; }
fails=0
check() {  # check <name> <command...>
  local name="$1"; shift
  if "$@"; then echo "  PASS: $name"; else
    echo "  FAIL: $name"; echo "$out" | sed 's/^/    out: /' | tail -n 25; fails=$((fails + 1)); fi
}
queued() { og log --reverse --format='%(trailers:key=PR,valueonly,separator=)' master..eval-rc-q/current 2>/dev/null | paste -sd' ' -; }
on_master() { og log --reverse --format=%s master | tail -n +2 | paste -sd'|' -; }
label() { cat "$S/batch/label" 2>/dev/null || echo none; }
says() { grep -qiF -- "$1" <<<"$out"; }
is() { [ "$1" = "$2" ] || { echo "    got [$1], want [$2]"; return 1; }; }

echo "=== eval-rc-q tests (v2) ==="

# AT1 enqueue.
fresh; add_pr 11 f11.txt
mesh merge-pr "$(url 11)"
check "AT1: merge-pr queues the PR: eval-rc-q/current is master plus its commit, with PR and PR-Head trailers" \
  bash -c '[ "$0" -eq 0 ] && [ "$(git --git-dir="$ORIGIN" rev-parse eval-rc-q/current~1)" = "$(git --git-dir="$ORIGIN" rev-parse master)" ] \
    && git --git-dir="$ORIGIN" log -1 --format=%B eval-rc-q/current | grep -qx "PR-Head: $(git --git-dir="$ORIGIN" rev-parse refs/pull/11/head)" \
    && git --git-dir="$ORIGIN" log -1 --format=%s eval-rc-q/current | grep -qx "feature\[PMAT-11\]: did thing 11 (#11)"' "$code"
check "AT1: the PR stays open against master, master is unchanged, and the draft batch PR is eval-rc:filling" \
  bash -c '[ "$(cat "$0/pr/11/state")" = OPEN ] && [ "$(git --git-dir="$ORIGIN" rev-list --count master)" = 1 ] && [ "$(cat "$0/batch/label")" = filling ]' "$S"
mesh merge-pr "$(url 11)"
check "AT1: queuing the same PR again changes nothing" \
  bash -c '[ "$0" -eq 0 ] && grep -q "Already queued" <<<"$1" && [ "$(git --git-dir="$ORIGIN" rev-list --count master..eval-rc-q/current)" = 1 ]' "$code" "$out"

# AT2 refusals: agent PR without smoke, a conflict with the queue, gating, full.
add_pr 12 "$AGENT/stage2.py"
mesh merge-pr "$(url 12)"
check "AT2: an agent PR without evals-smoke = success is refused and the queue is unchanged" \
  bash -c '[ "$0" -ne 0 ] && grep -q "mesh run-evals --smoke" <<<"$1" && [ "$2" = 11 ]' "$code" "$out" "$(queued)"
smoke_ok 12; mesh merge-pr "$(url 12)"
check "AT2: with evals-smoke = success it is queued" is "$(queued)" "11 12"
add_pr 13 f11.txt "a different f11"
mesh merge-pr "$(url 13)"
check "AT2: a PR that conflicts with the queue is refused; the queue is unchanged" \
  bash -c '[ "$0" -ne 0 ] && grep -qi "conflicts with the queue" <<<"$1" && [ "$2" = "11 12" ]' "$code" "$out" "$(queued)"
echo gating > "$S/batch/label"; add_pr 14 f14.txt
mesh merge-pr "$(url 14)"
check "AT2: while the queue is gating nothing is queued, and the batch PR is named" \
  bash -c '[ "$0" -ne 0 ] && grep -qi "gating" <<<"$1" && grep -qF "$2" <<<"$1" && [ "$3" = "11 12" ]' "$code" "$out" "$BATCH" "$(queued)"
echo filling > "$S/batch/label"; add_pr 15 f15.txt
mesh merge-pr "$(url 14)"
TTY=1 ANSWER=y mesh merge-pr "$(url 15)"
check "AT2: the 4th PR offers 'Queue full: gate it now?' and on yes runs mesh eval-rc-q gate" \
  bash -c '[ "$0" -eq 0 ] && grep -qi "Queue full: gate it now" <<<"$1" && grep -qx "eval-rc-q gate" "$2"' "$code" "$out" "$STUB_LOG"
add_pr 16 f16.txt; mesh merge-pr "$(url 16)"
check "AT2: a 5th PR is refused while the queue is full" bash -c '[ "$0" -ne 0 ] && grep -qi "full" <<<"$1"' "$code" "$out"

mesh eval-rc-q status
check "status prints the state, the batch and the batch PR" \
  bash -c 'grep -q "filling" <<<"$0" && grep -q "4/4" <<<"$0" && grep -qF "#12 at" <<<"$0" && grep -qF "$1" <<<"$0"' "$out" "$BATCH"

# AT3 pass: four PRs (one an agent PR), tests and evals pass, each squash-merged in order.
gated_tree="$(og rev-parse 'eval-rc-q/current^{tree}')"
mesh eval-rc-q gate
check "AT3: a passing gate runs the tests, then the full evals on the batch PR" \
  bash -c '[ "$0" -eq 0 ] && [ "$(grep -n "^tests" "$1" | head -1 | cut -d: -f1)" -lt "$(grep -n "^run-evals" "$1" | head -1 | cut -d: -f1)" ] && grep -qx "run-evals $2" "$1"' "$code" "$STUB_LOG" "$BATCH"
check "AT3: master gets one squash commit per ticket, in queue order, titled as the PRs" \
  is "$(on_master)" "feature[PMAT-11]: did thing 11 (#11)|feature[PMAT-12]: did thing 12 (#12)|feature[PMAT-14]: did thing 14 (#14)|feature[PMAT-15]: did thing 15 (#15)"
check "AT3: master's tree is the gated tree" is "$(og rev-parse 'master^{tree}')" "$gated_tree"
check "AT3: each PR was updated with master and marked eval-rc-q = success (the agent PR also evals = success) before its merge" \
  bash -c 'grep -q "pulls/15/update-branch" "$0" && grep -qx "eval-rc-q success" "$1/status/$(git --git-dir="$ORIGIN" rev-parse refs/pull/15/head)" \
    && grep -qx "evals success" "$1/status/$(git --git-dir="$ORIGIN" rev-parse refs/pull/12/head)"' "$STUB_LOG" "$S"
check "AT3: the batch is tagged eval-rc-q-<date>-1 on master, the queue deleted and the batch PR closed" \
  bash -c '[ "$(git --git-dir="$ORIGIN" rev-parse "refs/tags/eval-rc-q-$0-1")" = "$(git --git-dir="$ORIGIN" rev-parse master)" ] \
    && ! git --git-dir="$ORIGIN" rev-parse -q --verify eval-rc-q/current >/dev/null && [ ! -d "$1/batch" ]' "$TODAY" "$S"
check "AT3: each PR got a 'Gated in eval-rc-q batch' comment" bash -c '[ "$(grep -c "Gated in eval-rc-q batch" "$0/comments")" = 4 ]' "$S"

# AT4 one bad PR among four: fluke check reproduces it, bisect finds it in 2 steps, dropped, the other 3 promoted.
fresh; add_pr 21 f21.txt; add_pr 22 f22.txt; add_pr 23 BAD; add_pr 24 f24.txt; add_pr 20 "$AGENT/z.py"
smoke_ok 20; queue_all 20 21 23 24
mesh eval-rc-q gate
check "AT4: the bad PR is dropped and the other three are squash-merged" \
  bash -c '[ "$0" -eq 0 ] && [ "$1" = "feature[PMAT-20]: did thing 20 (#20)|feature[PMAT-21]: did thing 21 (#21)|feature[PMAT-24]: did thing 24 (#24)" ] \
    && [ "$(cat "$2/pr/23/state")" = OPEN ]' "$code" "$(on_master)" "$S"
check "AT4: one fluke re-run and at most 2 bisect runs, all on the regressed cases only" \
  bash -c '[ "$(grep -c "^run-evals --cases .* --run fluke-pr90-" "$0")" = 1 ] && [ "$(grep -c "^run-evals --cases .* --run bisect-pr90-" "$0")" -le 2 ]' "$STUB_LOG"
check "AT4: the dropped PR gets a comment and a Jira note naming its ticket and the failing cases" \
  bash -c 'grep -q "^https://github.com/q/r/pull/23 \*\*Dropped from the eval-rc-q batch" "$0/comments" \
    && grep -q "^Jira: PMAT-23" "$1/drops/23.md" && grep -q "sec-001, log-002" "$1/drops/23.md"' "$S" "$MESH_EVAL_RC_Q_STATE"
check "AT4: the re-gate after the drop ran the full evals again" bash -c '[ "$(grep -c "^run-evals https://github.com/q/r/pull/90" "$0")" = 2 ]' "$STUB_LOG"
mesh eval-rc-q status
check "status lists the Jira note still to post" says "drops/23.md"

# AT5 a fluke: the failing cases pass on a re-run, so the batch is gated again (attempt 2), nothing dropped.
fresh; add_pr 25 FLAKY; add_pr 26 "$AGENT/w.py"; smoke_ok 26; queue_all 25 26
mesh eval-rc-q gate
check "AT5: a fluke re-gates the batch as attempt 2 and drops nothing" \
  bash -c '[ "$0" -eq 0 ] && grep -q "^run-evals --attempt 2 https://github.com/q/r/pull/90" "$1" && ! grep -q "Dropped" "$2/comments" \
    && [ "$(git --git-dir="$ORIGIN" rev-list --count master)" = 3 ]' "$code" "$STUB_LOG" "$S"

# AT6 a queued PR that moved is dropped; the rest are re-gated and promoted.
fresh; add_pr 31 f31.txt; add_pr 32 f32.txt; queue_all 31 32; push_pr 31
mesh eval-rc-q gate
check "AT6: a PR pushed to after it was queued is dropped, and the rest promoted" \
  bash -c '[ "$0" -eq 0 ] && [ "$1" = "feature[PMAT-32]: did thing 32 (#32)" ] && grep -q "pull/31 \*\*Dropped.*moved after it was queued" "$2/comments"' \
  "$code" "$(on_master)" "$S"

# AT7 tree mismatch at promotion: the PR is dropped before its merge.
fresh; add_pr 41 f41.txt; add_pr 42 f42.txt; queue_all 41 42
STUB_UPDATE_EXTRA=42 mesh eval-rc-q gate
check "AT7: a PR whose updated tree is not the gated tree is dropped before its merge; the rest stay merged" \
  bash -c '[ "$0" -eq 0 ] && [ "$1" = "feature[PMAT-41]: did thing 41 (#41)" ] && [ "$(cat "$2/pr/42/state")" = OPEN ] \
    && grep -q "pull/42 \*\*Dropped.*not the tree that was gated" "$2/comments"' "$code" "$(on_master)" "$S"

# AT8 a hotfix merges straight into master; the next gate rebuilds the queue on it.
fresh; add_pr 51 f51.txt; queue_all 51
add_pr 52 hot.txt "" hotfix/PMAT-52-broken; mesh merge-pr "$(url 52)"
check "AT8: a hotfix/* PR squash-merges straight into master" is "$(on_master)" "feature[PMAT-52]: did thing 52 (#52)"
mesh eval-rc-q gate
check "AT8: the next gate rebuilds the queue on the new master and promotes it" \
  bash -c '[ "$0" -eq 0 ] && grep -q "Rebuilt" <<<"$1" && [ "$2" = "feature[PMAT-52]: did thing 52 (#52)|feature[PMAT-51]: did thing 51 (#51)" ]' \
  "$code" "$out" "$(on_master)"
add_pr 53 "$AGENT/x.py" "" hotfix/PMAT-53-agent; mesh merge-pr "$(url 53)"
check "AT8: an agent hotfix still needs the full evals status, as today" \
  bash -c '[ "$0" -ne 0 ] && grep -qF "mesh run-evals https://github.com/q/r/pull/53" <<<"$1"' "$code" "$out"

# Failing tests: bisected on the tests, the PR dropped, the rest promoted; no evals for a batch with no agent file.
fresh; add_pr 61 f61.txt; add_pr 62 TESTFAIL; add_pr 63 f63.txt; queue_all 61 62 63
mesh eval-rc-q gate
check "tests that fail are bisected to the PR, which is dropped; the rest promoted, with no evals (no agent file)" \
  bash -c '[ "$0" -eq 0 ] && [ "$1" = "feature[PMAT-61]: did thing 61 (#61)|feature[PMAT-63]: did thing 63 (#63)" ] && ! grep -q "^run-evals" "$2"' \
  "$code" "$(on_master)" "$STUB_LOG"

# AT13 a gate stopped by the quota keeps gating and resumes on the same head.
fresh; add_pr 71 "$AGENT/y.py"; smoke_ok 71; queue_all 71
head_before="$(og rev-parse eval-rc-q/current)"
STUB_QUOTA_ONCE=1 mesh eval-rc-q gate
check "AT13: a gate stopped by the quota exits 75, keeps eval-rc:gating, merges nothing, and says it resumes" \
  bash -c '[ "$0" -eq 75 ] && [ "$1" = gating ] && [ "$(git --git-dir="$ORIGIN" rev-list --count master)" = 1 ] && grep -qi "resume" <<<"$2"' "$code" "$(label)" "$out"
STUB_QUOTA_ONCE=1 mesh eval-rc-q gate --due
check "AT13: gate --due resumes a gating queue on the same head and promotes it" \
  bash -c '[ "$0" -eq 0 ] && grep -q "^run-evals https://github.com/q/r/pull/90" "$1" && [ "$(git --git-dir="$ORIGIN" log -1 --format=%s master)" = "feature[PMAT-71]: did thing 71 (#71)" ]' "$code" "$STUB_LOG"
check "AT13: the resumed gate evaluated the same queue head" bash -c 'grep -qx "evals success" "$0/status/$1"' "$S" "$head_before"

# gate --due: nothing without a queue; a filling queue with a PR is gated.
fresh; mesh eval-rc-q gate --due
check "gate --due with no queue does nothing" bash -c '[ "$0" -eq 0 ] && grep -qi "nothing to gate" <<<"$1"' "$code" "$out"
add_pr 81 f81.txt; queue_all 81; mesh eval-rc-q gate --due
check "gate --due gates a filling queue that holds a PR" bash -c '[ "$0" -eq 0 ] && [ "$(git --git-dir="$ORIGIN" rev-list --count master)" = 2 ]' "$code"

# Every PR dropped: the batch PR is closed and the queue deleted.
fresh; add_pr 91 TESTFAIL; queue_all 91
mesh eval-rc-q gate
check "a batch whose every PR is dropped is closed, the queue deleted, master unchanged" \
  bash -c '[ "$0" -ne 0 ] && [ ! -d "$1/batch" ] && ! git --git-dir="$ORIGIN" rev-parse -q --verify eval-rc-q/current >/dev/null \
    && [ "$(git --git-dir="$ORIGIN" rev-list --count master)" = 1 ]' "$code" "$S"

# R1 the failing cases also fail on master alone: no PR is to blame. Stop for the owner, drop nothing.
fresh; add_pr 101 "$AGENT/a.py"; add_pr 102 f102.txt; smoke_ok 101; queue_all 101 102
STUB_DRIFT=1 mesh eval-rc-q gate
check "R1: when the failing cases fail on master too, the gate stops, drops nothing, stays gating, and leaves a note for PMAT-358" \
  bash -c '[ "$0" -ne 0 ] && [ "$1" = gating ] && ! grep -q "Dropped" "$2/comments" && [ "$(git --git-dir="$ORIGIN" rev-list --count master)" = 1 ] \
    && grep -q -- "--run base-pr90-" "$3" && grep -l "^Jira: PMAT-358" "$4"/notes/*.md >/dev/null && ! grep -q "bisect-pr90" "$3"' \
  "$code" "$(label)" "$S" "$STUB_LOG" "$MESH_EVAL_RC_Q_STATE"

# R2 a failure on the judge's means alone: no case to bisect on, so stop for the owner.
fresh; add_pr 111 "$AGENT/b.py"; smoke_ok 111; queue_all 111
STUB_MEANS=1 mesh eval-rc-q gate
check "R2: a failure on the judge's means alone stops for the owner, with nothing dropped and no case run" \
  bash -c '[ "$0" -ne 0 ] && [ "$1" = gating ] && ! grep -q "Dropped" "$2/comments" 2>/dev/null && ! grep -q "^run-evals --cases" "$3" \
    && grep -q "judge" <<<"$4"' "$code" "$(label)" "$S" "$STUB_LOG" "$out"

# R4 a hotfix lands while the evals run: promote merges nothing, rebuilds on it and gates again.
fresh; add_pr 121 "$AGENT/c.py"; add_pr 122 f122.txt; smoke_ok 121; queue_all 121 122
STUB_HOTFIX_DURING_EVALS=1 mesh eval-rc-q gate
check "R4: a hotfix during the evals: nothing merged on that gate, the queue rebuilt on it, gated again, then promoted" \
  bash -c '[ "$0" -eq 0 ] && [ "$(grep -c "^run-evals https://github.com/q/r/pull/90" "$1")" = 2 ] \
    && [ "$2" = "hotfix during evals|feature[PMAT-121]: did thing 121 (#121)|feature[PMAT-122]: did thing 122 (#122)" ]' \
  "$code" "$STUB_LOG" "$(on_master)"
# R4 a hotfix lands between two merges: the rest are rebuilt on it and gated again.
fresh; add_pr 125 "$AGENT/d.py"; add_pr 126 f126.txt; smoke_ok 125; queue_all 125 126
STUB_HOTFIX_AFTER_MERGE=125 mesh eval-rc-q gate
check "R4: a hotfix between two merges: the next PR is not merged then; it is rebuilt on master, gated again and promoted" \
  bash -c '[ "$0" -eq 0 ] && [ "$1" = "feature[PMAT-125]: did thing 125 (#125)|hotfix hot-after-merge.txt|feature[PMAT-126]: did thing 126 (#126)" ] \
    && grep -q "moved during the promotion" <<<"$2"' "$code" "$(on_master)" "$out"

# R5 a promotion killed after its first merge carries on, with no new gate.
fresh; add_pr 131 "$AGENT/e.py"; add_pr 132 f132.txt; add_pr 133 f133.txt; smoke_ok 131; queue_all 131 132 133
gated_tree="$(og rev-parse 'eval-rc-q/current^{tree}')"
STUB_MERGE_FAIL_ONCE=132 mesh eval-rc-q gate
check "R5: a promotion killed partway stops with #131 merged and the queue still gating" \
  bash -c '[ "$0" -ne 0 ] && [ "$1" = "feature[PMAT-131]: did thing 131 (#131)" ] && [ "$2" = gating ]' "$code" "$(on_master)" "$(label)"
mesh eval-rc-q gate
check "R5: the rerun skips the merged PR and carries on: no tests, no evals, no rebuild; master ends at the gated tree" \
  bash -c '[ "$0" -eq 0 ] && ! grep -q "^run-evals\|^tests" "$1" && grep -q "Carrying on a promotion" <<<"$2" && ! grep -q "Rebuilt" <<<"$2" \
    && [ "$3" = "feature[PMAT-131]: did thing 131 (#131)|feature[PMAT-132]: did thing 132 (#132)|feature[PMAT-133]: did thing 133 (#133)" ] \
    && [ "$(git --git-dir="$ORIGIN" rev-parse "master^{tree}")" = "$4" ]' "$code" "$STUB_LOG" "$out" "$(on_master)" "$gated_tree"

# R7a the update-branch push started evals-gate before the statuses: it failed, so it is re-run before the merge.
fresh; add_pr 141 f141.txt; queue_all 141
STUB_CHECK_FAIL_FIRST=1 mesh eval-rc-q gate
check "R7: an evals-gate run that failed before the statuses is re-run, and the PR is merged only after it passes" \
  bash -c '[ "$0" -eq 0 ] && [ "$(grep -n "^gh run rerun" "$1" | cut -d: -f1)" -lt "$(grep -n "^gh pr merge" "$1" | cut -d: -f1)" ]' "$code" "$STUB_LOG"

# R8b a queue commit is what git merge --squash gives: two PRs editing different hunks of one file.
fresh
printf 'l1\nl2\nl3\nl4\nl5\nl6\n' > "$WORK/seed/multi.txt"; git -C "$WORK/seed" checkout -q master; git -C "$WORK/seed" add -A
git -C "$WORK/seed" commit -qm multi; git -C "$WORK/seed" push -q "$ORIGIN" master
add_pr 151 multi.txt "$(printf 'X\nl2\nl3\nl4\nl5\nl6')"; add_pr 152 multi.txt "$(printf 'l1\nl2\nl3\nl4\nl5\nY')"; queue_all 151 152
git clone -q "$ORIGIN" "$WORK/sq"; git -C "$WORK/sq" fetch -q origin refs/pull/152/head eval-rc-q/current
git -C "$WORK/sq" checkout -q --detach origin/eval-rc-q/current~1; git -C "$WORK/sq" merge -q --squash "$(og rev-parse refs/pull/152/head)" >/dev/null
check "R8: the queue commit's tree is the tree git merge --squash gives for the same PR" \
  is "$(git -C "$WORK/sq" write-tree)" "$(og rev-parse 'eval-rc-q/current^{tree}')"
rm -rf "$WORK/sq"

# R9 no queued PR changes an agent file: merge-pr gates at once; an agent PR waits for the batch.
fresh; add_pr 161 f161.txt; mesh merge-pr "$(url 161)"
check "R9: queuing a non-agent PR into a queue with no agent PR gates at once" bash -c '[ "$0" -eq 0 ] && grep -qx "eval-rc-q gate" "$1"' "$code" "$STUB_LOG"
fresh; add_pr 162 "$AGENT/f.py"; smoke_ok 162; add_pr 163 f163.txt; mesh merge-pr "$(url 162)"; mesh merge-pr "$(url 163)"
check "R9: with an agent PR in the queue, a non-agent PR waits for the batch" bash -c '[ "$0" -eq 0 ] && ! grep -q "^eval-rc-q gate" "$1"' "$code" "$STUB_LOG"

# A repo with no queue merges straight into its base, as before.
printf '{"queues": {}}\n' > "$MESH_MERGE_QUEUE_CONFIG"
fresh; add_pr 95 f95.txt; STUB_NO_QUEUE=1 mesh merge-pr "$(url 95)"
check "a repo with no queue in config/merge-queue.json merges into its base as before" \
  bash -c '[ "$0" -eq 0 ] && [ "$1" = "feature[PMAT-95]: did thing 95 (#95)" ] && ! grep -q "eval-rc-q" "$2"' "$code" "$(on_master)" "$STUB_LOG"
queue_config

[ "$fails" -eq 0 ] || exit 1
