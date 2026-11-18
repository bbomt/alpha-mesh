#!/usr/bin/env bash
# alpha-mesh/test/test-review-pull-request.sh
# Runs the review-pull-request pytest suite and checks the mesh entry point.

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$MESH_DIR/agents/review-pull-request"

echo "=== review-pull-request tests ==="
echo ""

echo "[1/4] pytest suite..."
if (cd "$AGENT_DIR" && uv run pytest -q); then
  echo "  PASS"
else
  echo "  FAIL: pytest suite failed"
  exit 1
fi

echo "[2/4] python entry point runs (--help)..."
if (cd "$AGENT_DIR" && uv run python -m review_pull_request --help) 2>&1 | grep -q "usage:"; then
  echo "  PASS"
else
  echo "  FAIL: python -m review_pull_request --help did not print usage"
  exit 1
fi

TEST_LOG_DIR="$(mktemp -d)"   # keep test output out of the real daily review log
trap 'rm -rf "$TEST_LOG_DIR"' EXIT

echo "[3/4] mesh wrapper runs end to end (--help)..."
if ALPHA_MESH_LOG_DIR="$TEST_LOG_DIR/smoke" "$MESH_DIR/bin/mesh" review-pull-request --help 2>&1 | grep -q "usage:"; then
  echo "  PASS"
else
  echo "  FAIL: mesh review-pull-request --help did not print usage"
  exit 1
fi

# The wrapper tees into a per-day log; it must append, or any run (including a
# --help smoke test) would wipe the day's earlier review log.
echo "[4/4] wrapper appends to the daily log instead of truncating it..."
ALPHA_MESH_LOG_DIR="$TEST_LOG_DIR" "$AGENT_DIR/bin/review-pull-request.sh" --help >/dev/null 2>&1 || true
size_after_first="$( (cat "$TEST_LOG_DIR"/*.log 2>/dev/null || true) | wc -c)"
ALPHA_MESH_LOG_DIR="$TEST_LOG_DIR" "$AGENT_DIR/bin/review-pull-request.sh" --help >/dev/null 2>&1 || true
size_after_second="$( (cat "$TEST_LOG_DIR"/*.log 2>/dev/null || true) | wc -c)"
if [[ "$size_after_first" -gt 0 && "$size_after_second" -gt "$size_after_first" ]]; then
  echo "  PASS"
else
  echo "  FAIL: log did not grow across two runs ($size_after_first -> $size_after_second bytes); the wrapper must append"
  exit 1
fi

echo ""
echo "=== ALL REVIEW-PULL-REQUEST TESTS PASSED ==="
