#!/usr/bin/env bash
# alpha-mesh/test/run-all.sh
# Runs all individual test suites

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$MESH_DIR"

FAILED=0

echo "========================================"
echo " alpha-mesh: running all tests"
echo "========================================"
echo ""

for test_script in "$MESH_DIR/test/test-"*; do
  name=$(basename "$test_script")
  if [[ "$name" == "run-all.sh" ]]; then
    continue
  fi

  echo "--- running $name ---"
  if bash "$test_script"; then
    echo ""
  else
    echo ""
    echo "FAIL: $name failed"
    FAILED=1
  fi
done

echo "========================================"
if [[ "$FAILED" -eq 0 ]]; then
  echo " ALL TESTS PASSED"
  exit 0
else
  echo " SOME TESTS FAILED"
  exit 1
fi