#!/usr/bin/env bash
# alpha-mesh/test/test-run-evals.sh
# Runs `mesh run-evals` against stand-in `gh`, `git` and `uv` and a stand-in eval run, and checks that it posts the
# gate's verdict as the `evals` status on the PR's head commit (pending first), comments the report, scores with
# master's baseline and tolerance, refuses a stale branch, and leaves the eval runs folder alone (PMAT-312). Also
# `mesh run-evals --force-success` (evals = success by hand). Each post of
# `evals` re-runs the GitHub check `evals-gate` (gh run rerun), so it reads the new status without a push.
# PMAT-358: `--smoke` (k=1 on master's eval/smoke.json, status `evals-smoke`); one run at a time (a lock); a run
# starts only when the free-model quota left (a stand-in key endpoint) covers it, and logs it before and after;
# the OpenRouter token is never printed.

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
URL="https://github.com/o/r/pull/15"
HEAD_SHA="abc1234def5678abc1234def5678abc1234def56"
CANARY="sk-or-CANARY-$RANDOM-never-printed"
export ALPHA_MESH_OPENROUTER_TOKEN="$CANARY"
export MESH_OPENROUTER_KEY_URL="file://$WORK/key.json" STUB_KEY="$WORK/key.json"
export MESH_RUN_EVALS_LOCK="$WORK/run-evals.lock"
key() {  # key <remaining>: the stand-in /api/v1/key answer
  printf '{"data":{"free_model_daily_requests":{"used":%s,"limit":1000,"remaining":%s}}}\n' "$((1000 - $1))" "$1" > "$STUB_KEY"
}

mkdir -p "$WORK/bin" "$WORK/home/.mesh/eval-runs/keep-me"
cat > "$WORK/bin/gh" <<'EOF'
#!/usr/bin/env bash
echo "gh $*" >> "$STUB_LOG"
case "$1 $2" in
  "pr view") printf '{"state":"OPEN","headRefOid":"%s","baseRefName":"master"}\n' "$STUB_HEAD" ;;
  "api repos/o/r/compare/master...$STUB_HEAD") printf '{"behind_by":%s}\n' "$STUB_BEHIND" ;;
  "api repos/o/r/statuses/$STUB_HEAD") ;;
  "pr comment")  # pr comment <url> --body-file <file> | --body <text>
    if [ "$4" = --body-file ]; then cp "$5" "$STUB_COMMENT"; else printf '%s\n' "$5" > "$STUB_COMMENT"; fi ;;
  "pr diff") printf '%s\n' "agents/review-pull-request/src/review_pull_request/stage2.py" ;;
  "api repos/o/r/commits/master/status") printf '{"statuses":[]}\n' ;;
  "api repos/o/r/commits/$STUB_HEAD/status") printf '{"statuses":[]}\n' ;;
  "api repos/o/r/pulls/15/commits?per_page=100") printf '[{"sha":"%s"}]\n' "$STUB_HEAD" ;;
  "run list") echo "${STUB_RUN_ID-4242}" ;;  # the newest evals-gate check run on the head ("" when none)
  "run rerun") ;;
  *) echo "unexpected gh call: $*" >&2; exit 9 ;;
esac
EOF
cat > "$WORK/bin/git" <<'EOF'
#!/usr/bin/env bash
shift 2  # -C <dir>
echo "git $*" >> "$STUB_LOG"
case "$1" in
  fetch) ;;
  show)
    case "$2" in
      *baseline.json) [ -z "${STUB_NO_BASELINE:-}" ] || exit 128  # 65 development cases, like the real one
        printf '{"model": "master-baseline", "cases": {%s}}\n' "$(seq -f '"c%g": {}' 1 65 | paste -sd, -)" ;;
      origin/master:*smoke.json) echo '{"cases": ["sec-001", "R45-fee8a9a-001"]}' ;;
      origin/master:*score_eval_run.py)  # STUB_NO_GATE: the base has no gate (renamed or deleted)
        [ -z "${STUB_NO_GATE:-}" ] || exit 128; printf '"""doc"""\nTOLERANCE = 7  # runs\n' ;;
      *score_eval_run.py) printf '"""doc"""\nTOLERANCE = 9  # runs\n' ;;
      origin/master:*write_baseline.py) [ -z "${STUB_NO_GATE:-}" ] || exit 128; echo '# master write_baseline' ;;
      *) exit 128 ;;
    esac ;;
  rev-parse) echo "$STUB_HEAD" ;;
  checkout) ;;
  worktree)
    if [ "$2" = add ]; then  # worktree add -q --detach <dir> <sha>
      mkdir -p "$5/agents/review-pull-request/eval/scripts"
      # The stand-in eval: logs its call and spends 330 of the quota (the key endpoint then answers 670).
      printf '#!/usr/bin/env bash\necho "run_eval $*" >> "$STUB_LOG"\nsed -i "s/\\"remaining\\":[0-9]*/\\"remaining\\":670/" "$STUB_KEY"\nexit 100\n' \
        > "$5/agents/review-pull-request/eval/scripts/run_eval.sh"
      chmod +x "$5/agents/review-pull-request/eval/scripts/run_eval.sh"
    else  # worktree remove --force <dir>
      rm -rf "$4"
    fi ;;
  *) echo "unexpected git call: $*" >&2; exit 9 ;;
esac
EOF
cat > "$WORK/bin/uv" <<'EOF'
#!/usr/bin/env bash
echo "uv $*" >> "$STUB_LOG"
[ "$1" = sync ] && exit 0
# uv run python eval/scripts/score_eval_run.py <run> --baseline <f> --tolerance <t> --markdown <md> [--quick --cases <f>]
cp "$6" "$STUB_BASELINE_SEEN"
[ "${12:-}" = --cases ] && cp "${13}" "$STUB_BASELINE_SEEN.cases"
[ "${11:-}" = --cases ] && cp "${12}" "$STUB_BASELINE_SEEN.cases"
[ "${11:-}" = --regressed ] && echo "${12}" > "$STUB_BASELINE_SEEN.regressed"
cat eval/scripts/score_eval_run.py eval/scripts/write_baseline.py > "$STUB_BASELINE_SEEN.scorer" 2>/dev/null || true
case "$STUB_GATE" in
  pass) echo "runs passed 142/195 · baseline 140/195 · needs at least 133/195 (tolerance 7) · change +2" > "${10}"; exit 0 ;;
  fail) echo "runs passed 90/195 · baseline 140/195 · needs at least 133/195 (tolerance 7) · change -50" > "${10}"; exit 1 ;;
  crash) exit 2 ;;
  *) echo "unexpected STUB_GATE: $STUB_GATE" >&2; exit 99 ;;
esac
EOF
chmod +x "$WORK/bin/"*

fails=0
run() {  # run <name> <behind> <gate> <want exit> <want status sequence> <want comment yes|no>
  local name="$1" out code statuses commented=no
  : > "$WORK/log"; rm -f "$WORK/comment" "$WORK/baseline-seen"; key "${REMAINING:-1000}"
  set +e
  out="$(PATH="$WORK/bin:$PATH" HOME="$WORK/home" MESH_RUN_EVALS_DIR="$WORK/gates" STUB_LOG="$WORK/log" \
         STUB_HEAD="$HEAD_SHA" STUB_BEHIND="$2" STUB_GATE="$3" STUB_COMMENT="$WORK/comment" \
         STUB_BASELINE_SEEN="$WORK/baseline-seen" "$MESH_DIR/bin/mesh" run-evals "$URL" 2>&1)"
  code=$?
  set -e
  statuses="$(grep -o 'state=[a-z]*' "$WORK/log" | cut -d= -f2 | paste -sd, - || true)"
  [ -f "$WORK/comment" ] && commented=yes
  local bad=""
  [ "$code" -eq "$4" ] || bad="exit $code, want $4"
  [ "$statuses" = "$5" ] || bad="$bad; statuses '$statuses', want '$5'"
  [ "$commented" = "$6" ] || bad="$bad; comment $commented, want $6"
  [ -d "$WORK/home/.mesh/eval-runs/keep-me" ] || bad="$bad; the eval runs folder was touched"
  if grep -qF -- "$CANARY" <<<"$out" || grep -rqF -- "$CANARY" "$WORK/log" "$WORK/home" \
     || { [ -f "$WORK/comment" ] && grep -qF -- "$CANARY" "$WORK/comment"; }; then
    bad="$bad; the OpenRouter token was printed or written"
  fi
  if [ -n "$bad" ]; then
    echo "  FAIL: $name: $bad"; echo "$out" | sed 's/^/    /'; sed 's/^/    log: /' "$WORK/log"
    fails=$((fails + 1)); return 1
  fi
  echo "  PASS: $name"
}

echo "=== run-evals tests ==="
run "a stale branch is refused before any run or status" 3 pass 1 "" no || true
if grep -q "run_eval" "$WORK/log"; then echo "  FAIL: a stale branch must not start the eval"; fails=$((fails + 1)); fi

if run "a passing run posts pending, then success, and comments the report" 0 pass 0 "pending,success" yes; then
  grep -q 'description=runs passed 142/195 · baseline 140/195 · needs at least 133/195 (tolerance 7) · change +2' "$WORK/log" \
    || { echo "  FAIL: the success status lacks the count"; fails=$((fails + 1)); }
  grep -q -- "--tolerance 7 " "$WORK/log" || { echo "  FAIL: not scored with master's tolerance"; fails=$((fails + 1)); }
  grep -q "TOLERANCE = 7" "$WORK/baseline-seen.scorer" && grep -q "# master write_baseline" "$WORK/baseline-seen.scorer" \
    || { echo "  FAIL: not scored with master's score_eval_run.py and write_baseline.py"; fails=$((fails + 1)); }
  grep -q '"master-baseline"' "$WORK/baseline-seen" \
    || { echo "  FAIL: not scored against master's baseline"; fails=$((fails + 1)); }
  grep -q -- "--run gate-pr15-abc1234 --judge-repeats all" "$WORK/log" \
    || { echo "  FAIL: the eval did not judge every repeat"; fails=$((fails + 1)); }
  grep -q "abc1234" "$WORK/comment" || { echo "  FAIL: the comment does not name the commit"; fails=$((fails + 1)); }
  [ ! -e "$WORK/gates/${HEAD_SHA:0:12}" ] || { echo "  FAIL: the worktree was left behind"; fails=$((fails + 1)); }
  grep -q "^gh run list -R o/r --workflow evals-gate.yml --commit $HEAD_SHA" "$WORK/log" \
    && grep -q "^gh run rerun 4242 -R o/r" "$WORK/log" \
    || { echo "  FAIL: posting evals did not re-run the evals-gate check"; fails=$((fails + 1)); }
fi
if STUB_RUN_ID="" run "with no evals-gate check run yet, it still posts evals and says the check runs on the next push" \
     0 pass 0 "pending,success" yes; then
  if grep -q "^gh run rerun" "$WORK/log"; then echo "  FAIL: re-ran a check run that does not exist"; fails=$((fails + 1)); fi
fi
run "a failing run posts failure and comments the report" 0 fail 1 "pending,failure" yes || true
run "a run the gate cannot score posts error, no comment" 0 crash 1 "pending,error" no || true
STUB_NO_BASELINE=1 run "a run that stops before a verdict posts error, never leaves pending" 0 pass 128 "pending,error" no || true
if STUB_NO_GATE=1 run "a base without score_eval_run.py posts error and never uses the head's tolerance" \
     0 pass 1 "pending,error" no; then
  grep -q "state=error -f context=evals -f description=origin/master has no score_eval_run.py" "$WORK/log" \
    || { echo "  FAIL: the error status does not name the missing score_eval_run.py"; fails=$((fails + 1)); }
  if grep -q "run_eval\|--tolerance" "$WORK/log"; then
    echo "  FAIL: ran or scored the eval without the base's tolerance"; fails=$((fails + 1))
  fi
fi


# --force-success: evals = success by hand, recorded on the PR.
: > "$WORK/log"; rm -f "$WORK/comment"
PATH="$WORK/bin:$PATH" STUB_LOG="$WORK/log" STUB_HEAD="$HEAD_SHA" STUB_COMMENT="$WORK/comment" \
  "$MESH_DIR/bin/mesh" run-evals --force-success "$URL" --reason "judge outage, scores read by hand" >/dev/null 2>&1
if grep -q "state=success -f context=evals -f description=forced: judge outage" "$WORK/log" \
   && grep -q "judge outage" "$WORK/comment"; then
  echo "  PASS: --force-success posts evals = success with the reason and records it on the PR"
else
  echo "  FAIL: --force-success"; sed 's/^/    log: /' "$WORK/log"; fails=$((fails + 1))
fi
if PATH="$WORK/bin:$PATH" STUB_LOG="$WORK/log" "$MESH_DIR/bin/mesh" run-evals --force-success "$URL" >/dev/null 2>&1; then
  echo "  FAIL: --force-success without a reason must be refused"; fails=$((fails + 1))
else
  echo "  PASS: --force-success without a reason is refused"
fi


# --smoke: k=1 on master's smoke list only, the first repeat judged, scored with --quick --cases; posts evals-smoke
# (pending, then the verdict) and a PR comment, and never the full `evals` status.
: > "$WORK/log"; rm -f "$WORK/comment" "$WORK/baseline-seen.cases"; key 1000
set +e
PATH="$WORK/bin:$PATH" HOME="$WORK/home" MESH_RUN_EVALS_DIR="$WORK/gates" STUB_LOG="$WORK/log" STUB_HEAD="$HEAD_SHA" \
  STUB_GATE=pass STUB_COMMENT="$WORK/comment" STUB_BASELINE_SEEN="$WORK/baseline-seen" \
  "$MESH_DIR/bin/mesh" run-evals --smoke "$URL" >"$WORK/smoke-out" 2>&1
code=$?
set -e
if [ "$code" -eq 0 ] \
   && grep -qF -- "--run smoke-pr15-abc1234 --k 1 --judge-repeats first --filter-pattern ^(sec-001|R45-fee8a9a-001)\$" "$WORK/log" \
   && grep -q -- "--quick --cases" <(grep "score_eval_run.py" "$WORK/log") \
   && grep -q '"R45-fee8a9a-001"' "$WORK/baseline-seen.cases" \
   && [ "$(grep -o 'state=[a-z]* -f context=[a-z-]*' "$WORK/log" | paste -sd, -)" = "state=pending -f context=evals-smoke,state=success -f context=evals-smoke" ] \
   && grep -q "smoke" "$WORK/comment"; then
  echo "  PASS: --smoke runs k=1 on master's smoke list, posts evals-smoke and comments, never evals"
else
  echo "  FAIL: --smoke (exit $code)"; sed "s/^/    out: /" "$WORK/smoke-out"; sed 's/^/    log: /' "$WORK/log"; fails=$((fails + 1))
fi
if grep -q "run-evals --quick\|--quick <PR url>" "$MESH_DIR/bin/run-evals.sh"; then
  echo "  FAIL: --quick is replaced by --smoke"; fails=$((fails + 1))
fi

# The quota: a run starts only when the free-model requests left today cover it; it logs them before and after.
if REMAINING=399 run "with fewer free requests left than a full run needs (400), it refuses before any status or run" \
     0 pass 75 "" no; then
  grep -q "run_eval" "$WORK/log" && { echo "  FAIL: started the eval below the quota threshold"; fails=$((fails + 1)); }
fi
rm -f "$WORK/home/.mesh/quota.log"
if run "a full run logs the free quota before and after it, and what it used" 0 pass 0 "pending,success" yes; then
  grep -q "run=gate-pr15-abc1234 mode=full before=1000$" "$WORK/home/.mesh/quota.log" \
    && grep -q "run=gate-pr15-abc1234 mode=full after=670 used=330$" "$WORK/home/.mesh/quota.log" \
    || { echo "  FAIL: quota not logged:"; sed 's/^/    /' "$WORK/home/.mesh/quota.log"; fails=$((fails + 1)); }
fi
rm -f "$STUB_KEY"
MESH_OPENROUTER_KEY_URL="file://$WORK/no-such-key.json" run "when the quota cannot be read, it refuses" 0 pass 75 "" no || true

# One run at a time: while another run holds the lock, a second one exits at once and starts nothing.
exec 8>"$MESH_RUN_EVALS_LOCK"; flock 8
if run "a second run while one holds the lock exits without a status or a run" 0 pass 75 "" no; then
  grep -q "run_eval" "$WORK/log" && { echo "  FAIL: started a second eval"; fails=$((fails + 1)); }
fi
exec 8>&-

# The full gate writes the cases it lost (regressed.json), for the eval-rc-q drop loop; --attempt makes a fresh run.
rm -f "$WORK/baseline-seen.regressed"
if run "a full run asks the scorer for the regressed cases" 0 pass 0 "pending,success" yes; then
  grep -q "/gate-pr15-abc1234/regressed.json$" "$WORK/baseline-seen.regressed" 2>/dev/null \
    || { echo "  FAIL: no --regressed <run>/regressed.json"; fails=$((fails + 1)); }
fi
: > "$WORK/log"; key 1000
set +e
PATH="$WORK/bin:$PATH" HOME="$WORK/home" MESH_RUN_EVALS_DIR="$WORK/gates" STUB_LOG="$WORK/log" STUB_HEAD="$HEAD_SHA" STUB_BEHIND=0 \
  STUB_GATE=pass STUB_COMMENT="$WORK/comment" STUB_BASELINE_SEEN="$WORK/baseline-seen" \
  "$MESH_DIR/bin/mesh" run-evals --attempt 2 "$URL" >/dev/null 2>&1; code=$?
set -e
if [ "$code" -eq 0 ] && grep -q -- "--run gate-pr15-abc1234-a2 --judge-repeats all" "$WORK/log"; then
  echo "  PASS: --attempt 2 runs the full evals as a fresh run (gate-pr15-abc1234-a2)"
else echo "  FAIL: --attempt (exit $code)"; sed 's/^/    log: /' "$WORK/log"; fails=$((fails + 1)); fi

# --cases: k=3 on the listed cases at a commit, scored with --cases, posting no status; the quota needed is pro rata.
printf '{"cases": ["sec-001", "log-002"]}\n' > "$WORK/cases.json"
cases_run() {  # cases_run <remaining>: sets code
  : > "$WORK/log"; key "$1"; rm -f "$WORK/baseline-seen.cases"
  set +e
  PATH="$WORK/bin:$PATH" HOME="$WORK/home" MESH_RUN_EVALS_DIR="$WORK/gates" STUB_LOG="$WORK/log" STUB_HEAD="$HEAD_SHA" \
    STUB_GATE="${GATE:-pass}" STUB_BASELINE_SEEN="$WORK/baseline-seen" \
    "$MESH_DIR/bin/mesh" run-evals --cases "$WORK/cases.json" --commit "$HEAD_SHA" --run fluke-x >"$WORK/cases-out" 2>&1
  code=$?
  set -e
}
cases_run 1000
if [ "$code" -eq 0 ] && grep -qF -- "--run fluke-x --judge-repeats all --filter-pattern ^(sec-001|log-002)\$" "$WORK/log" \
   && grep -q '"log-002"' "$WORK/baseline-seen.cases" && ! grep -q "statuses\|pr comment" "$WORK/log"; then
  echo "  PASS: --cases runs k=3 on the listed cases at the commit, scores them with --cases and posts nothing"
else echo "  FAIL: --cases (exit $code)"; sed 's/^/    out: /' "$WORK/cases-out"; sed 's/^/    log: /' "$WORK/log"; fails=$((fails + 1)); fi
GATE=fail cases_run 1000
[ "$code" -eq 1 ] && echo "  PASS: --cases exits 1 when the cases fail" || { echo "  FAIL: --cases fail exit $code"; fails=$((fails + 1)); }
cases_run 12   # 2 of 65 cases: ceil(400 * 2 / 65) = 13
if [ "$code" -eq 75 ] && ! grep -q run_eval "$WORK/log"; then
  echo "  PASS: --cases needs the full run's quota pro rata (13 for 2 of 65 cases): with 12 left it does not start"
else echo "  FAIL: --cases quota (exit $code)"; fails=$((fails + 1)); fi

# The help: --help prints it and exits 0; no arguments print it to stderr and exit 2. Both name every mode.
set +e
help_out="$("$MESH_DIR/bin/mesh" run-evals --help 2>/dev/null)"; help_code=$?
noargs_err="$("$MESH_DIR/bin/mesh" run-evals 2>&1 >/dev/null)"; noargs_code=$?
set -e
ok=1
for want in "mesh run-evals <PR url>" "--smoke <PR url>" "--force-success <PR url> --reason" "gh pr checks"; do
  grep -qF -- "$want" <<<"$help_out" && grep -qF -- "$want" <<<"$noargs_err" || ok=0
done
if [ "$ok" -eq 1 ] && [ "$help_code" -eq 0 ] && [ "$noargs_code" -eq 2 ]; then
  echo "  PASS: --help prints the help (exit 0); no arguments print it to stderr (exit 2)"
else
  echo "  FAIL: help (--help exit $help_code, no args exit $noargs_code)"; fails=$((fails + 1))
fi

[ "$fails" -eq 0 ] || exit 1
