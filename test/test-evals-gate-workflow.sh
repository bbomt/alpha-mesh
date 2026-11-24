#!/usr/bin/env bash
# alpha-mesh/test/test-evals-gate-workflow.sh
# Runs the `evals-gate` job's step (.github/workflows/evals-gate.yml) in a scratch repo and checks that it runs the
# base branch's check_pr_evals.py and list_evals_gated_files.py, not the PR's, so a PR cannot loosen the check
# that judges it; and that the job fails, never running the PR's copy, when the base has none (PMAT-312, PMAT-349).
# PMAT-358: a PR into master needs the commit status eval-rc-q = success on its head (posted only by the queue's
# promotion, mesh eval-rc-q), except a hotfix/* branch; a stand-in gh answers the status.

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
S=agents/review-pull-request/eval/scripts
fails=0

# The step's shell, as written in the workflow.
python3 - "$MESH_DIR/.github/workflows/evals-gate.yml" > "$WORK/step.sh" <<'PY'
import re, sys
text = open(sys.argv[1]).read()
multi = re.search(r"\n        run: \|\n((?:          .*\n|\n)+)", text)
body = "".join(l[10:] + "\n" for l in multi.group(1).splitlines()) if multi else \
    re.search(r"\n        run: (.+)\n", text).group(1) + "\n"
print("set -euo pipefail\n" + body)
PY

git_q() { git -C "$1" -c user.name=t -c user.email=t@t "${@:2}" >/dev/null 2>&1; }
# write_scripts <dir> <who>: stand-in scripts that report whose copy ran (and that both came from the same side)
write_scripts() {
  mkdir -p "$1/$S"
  printf 'import sys, pathlib\nsys.path.insert(0, str(pathlib.Path(__file__).parent))\nimport list_evals_gated_files as g\nprint("ran %s check_pr_evals with", g.WHO, sys.argv[1:])\n' "$2" > "$1/$S/check_pr_evals.py"
  printf 'WHO = "%s"\n' "$2" > "$1/$S/list_evals_gated_files.py"
}

# setup <base has scripts: yes|no>: an origin with master, and a PR checkout whose copies differ
setup() {
  rm -rf "$WORK/origin" "$WORK/pr"
  git init -q -b master "$WORK/origin"
  echo x > "$WORK/origin/README.md"
  [ "$1" = yes ] && write_scripts "$WORK/origin" base
  git_q "$WORK/origin" add -A && git_q "$WORK/origin" commit -m base
  git clone -q "$WORK/origin" "$WORK/pr"
  write_scripts "$WORK/pr" pr
  git_q "$WORK/pr" add -A && git_q "$WORK/pr" commit -m pr
}

mkdir -p "$WORK/bin"
cat > "$WORK/bin/gh" <<'EOF'
#!/usr/bin/env bash
# api repos/o/r/commits/<sha>/status: eval-rc-q = $STUB_RCQ (none when empty)
[ "$*" = "api repos/o/r/commits/h123/status" ] || { echo "unexpected gh call: $*" >&2; exit 9; }
if [ -n "${STUB_RCQ:-}" ]; then printf '{"statuses":[{"context":"eval-rc-q","state":"%s"}]}\n' "$STUB_RCQ"; else echo '{"statuses":[]}'; fi
EOF
chmod +x "$WORK/bin/gh"
run_step() { (cd "$WORK/pr" && PATH="$WORK/bin:$PATH" BASE_REF=master HEAD_REF="${HEAD_REF:-feature/PMAT-1-x}" HEAD_SHA=h123 \
  REPO=o/r STUB_RCQ="${RCQ-success}" PR_URL=https://github.com/o/r/pull/1 bash "$WORK/step.sh" 2>&1); }

setup yes
out="$(run_step)" || true
if grep -q "ran base check_pr_evals with base \['--evals-only', 'https://github.com/o/r/pull/1'\]" <<<"$out" \
   && grep -q "Using master's check_pr_evals.py" <<<"$out"; then
  echo "  PASS: runs the base branch's check_pr_evals.py and list_evals_gated_files.py, not the PR's"
else
  echo "  FAIL: base copy"; sed 's/^/    /' <<<"$out"; fails=$((fails + 1))
fi

setup no
code=0; out="$(run_step)" || code=$?
if [ "$code" -ne 0 ] && ! grep -q "ran pr check_pr_evals" <<<"$out" \
   && grep -q "ERROR: master has no $S/check_pr_evals.py" <<<"$out"; then
  echo "  PASS: when the base has no check_pr_evals.py, the job fails and never runs the PR's copy"
else
  echo "  FAIL: no base copy (exit $code)"; sed 's/^/    /' <<<"$out"; fails=$((fails + 1))
fi

setup yes
for rcq in "" failure pending; do
  code=0; out="$(RCQ="$rcq" run_step)" || code=$?
  if [ "$code" -ne 0 ] && ! grep -q "ran .* check_pr_evals" <<<"$out" && grep -q "mesh merge-pr queues it" <<<"$out"; then
    echo "  PASS: a PR into master without eval-rc-q = success (it is '${rcq:-missing}') fails and says it goes through the queue"
  else
    echo "  FAIL: eval-rc-q '${rcq:-missing}' (exit $code)"; sed 's/^/    /' <<<"$out"; fails=$((fails + 1))
  fi
done
out="$(RCQ=success run_step)" || true
grep -q "ran base check_pr_evals" <<<"$out" && echo "  PASS: with eval-rc-q = success, check_pr_evals then judges the PR as before" \
  || { echo "  FAIL: eval-rc-q success"; sed 's/^/    /' <<<"$out"; fails=$((fails + 1)); }
out="$(HEAD_REF=hotfix/PMAT-2-broken RCQ="" run_step)" || true
if grep -q "ran base check_pr_evals" <<<"$out"; then
  echo "  PASS: a hotfix/* PR needs no eval-rc-q and is judged by check_pr_evals as before"
else
  echo "  FAIL: hotfix"; sed 's/^/    /' <<<"$out"; fails=$((fails + 1))
fi

[ "$fails" -eq 0 ] || exit 1
