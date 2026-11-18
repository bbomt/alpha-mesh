#!/usr/bin/env bash
# alpha-mesh/test/test-bin-layout.sh
# PMAT-348: every bash script ends in .sh except the entry point bin/mesh, every mesh <command>
# still reaches its script, and nothing tracked calls a script by its old, extensionless path.

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$MESH_DIR"

FAILED=0
echo "=== bin layout tests ==="

echo "[1/3] every tracked bash script except bin/mesh ends in .sh..."
bad=""
while IFS= read -r f; do
  [[ -f "$f" && "$f" != *.sh && "$f" != bin/mesh ]] || continue
  if head -n 1 "$f" | grep -qE '^#!.*\b(bash|sh)\b'; then bad+=" $f"; fi
done < <(git ls-files)
if [[ -z "$bad" ]]; then echo "  PASS"; else echo "  FAIL: no .sh extension:$bad"; FAILED=1; fi

echo "[2/3] every mesh <command> reaches its script..."
commands=$(bin/mesh --help 2>&1 | sed -n 's/^Commands: //p' | tr -d '|')
[[ -n "$commands" ]] || { echo "  FAIL: no command list in mesh --help"; FAILED=1; }
for cmd in $commands; do
  if [[ "$cmd" == test ]]; then   # mesh test runs this whole suite: check its target instead
    target=$(sed -n 's/^ *test) .*exec "\$SCRIPTS_DIR\/\([^"]*\)".*/\1/p' bin/mesh)
    if [[ -x "bin/$target" ]]; then echo "  PASS: test"; else echo "  FAIL: test -> bin/$target missing"; FAILED=1; fi
    continue
  fi
  rc=0
  err=$(timeout 120 bin/mesh "$cmd" --help 2>&1 >/dev/null) || rc=$?
  if [[ "$rc" -eq 126 || "$rc" -eq 127 ]] || grep -q "No such file or directory" <<<"$err"; then
    echo "  FAIL: mesh $cmd (exit $rc): $(head -n 2 <<<"$err")"
    FAILED=1
  else
    echo "  PASS: $cmd"
  fi
done

echo "[3/3] no tracked file calls a script by its old path..."
# memory/ is history, and llm.py's dry-run overview is canned text describing an old PR (and an
# evals-gated file), not a caller.
stale=""
while IFS= read -r sh; do
  old="${sh%.sh}"
  short="$(basename "$(dirname "$old")")/$(basename "$old")"   # its folder and name, without .sh
  hits=$(git grep -nP "(?<![\w-])\Q$short\E(?![.\w-])" -- ':!memory' \
           ':!agents/review-pull-request/src/review_pull_request/llm.py' || true)
  [[ -z "$hits" ]] || stale+=$'\n'"$hits"
done < <(git ls-files '*.sh')
if [[ -z "$stale" ]]; then echo "  PASS"; else echo "  FAIL: old paths still referenced:$stale"; FAILED=1; fi

echo ""
if [[ "$FAILED" -eq 0 ]]; then
  echo "=== ALL BIN LAYOUT TESTS PASSED ==="
  exit 0
else
  echo "=== SOME BIN LAYOUT TESTS FAILED ==="
  exit 1
fi
