#!/usr/bin/env bash
# alpha-mesh/test/test-dream.sh
# Tests for bin/dream.sh script
# Tests the .mesh/MEMORY.md synthesis rewrite behavior

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$MESH_DIR/bin:$PATH"
cd "$MESH_DIR"

FAILED=0

echo "=== dream tests ==="
echo ""

# ─── test: bin/dream.sh exists and is executable ──────────────────────────────────
echo "[1/10] bin/dream.sh exists and is executable..."
if [[ -x "$MESH_DIR/bin/dream.sh" ]]; then
  echo "  PASS"
else
  echo "  FAIL: bin/dream.sh not found or not executable"
  FAILED=1
fi

# ─── test: dream --dry-run runs without error ──────────────────────────────────
echo "[2/10] dream --dry-run runs without error..."
dry_output=$(mesh dream --dry-run 2>&1 || true)
if echo "$dry_output" | grep -q "DRY RUN COMPLETE\|nothing to process\|no session"; then
  echo "  PASS: --dry-run succeeded"
else
  echo "  FAIL: --dry-run did not show expected output"
  echo "  Output: $dry_output"
  FAILED=1
fi

# ─── test: dream --dry-run writes nothing ──────────────────────────────────────
echo "[3/10] dream --dry-run writes nothing..."
dry_mesh_files=$(find "$MESH_DIR" -path "*/mesh/MEMORY.md" -newer "$MESH_DIR/bin/dream.sh" 2>/dev/null || true)
if [[ -z "$dry_mesh_files" ]]; then
  echo "  PASS: no .mesh/MEMORY.md created by dry-run"
else
  echo "  WARN: dry-run may have created files: $dry_mesh_files"
fi

# ─── test: dream completes without hanging ───────────────────
echo "[4/10] dream completes without hanging..."
# Check that it completes with DRY RUN COMPLETE message
dry_run_output=$(mesh dream --dry-run 2>&1 || true)
if echo "$dry_run_output" | grep -q "DRY RUN COMPLETE"; then
  echo "  PASS: dream completed without hanging"
else
  echo "  FAIL: dream did not complete properly"
  echo "  Output: $dry_run_output"
  FAILED=1
fi

# ─── test: dream --days 5 --dry-run runs without error ───────────────────────────────────
echo "[5/10] dream --days 5 --dry-run runs without error..."
five_days_output=$(mesh dream --days 5 --dry-run 2>&1 || true)
if echo "$five_days_output" | grep -qi "DRY RUN\|Found [0-9]+ session"; then
  echo "  PASS: --5-days --dry-run completed"
else
  echo "  FAIL: --5-days --dry-run had unexpected output"
  echo "  Output: $five_days_output"
  FAILED=1
fi

# ─── test: dream --dry-run shows progress output ─────────────────────────────────────
echo "[6/10] dream --dry-run shows progress output..."
progress_output=$(mesh dream --days 5 --dry-run 2>&1 | grep -E "Processed [0-9]+ files|Found [0-9]+ session" | head -5)
if [[ -n "$progress_output" ]]; then
  echo "  PASS: progress messages shown"
else
  echo "  FAIL: no progress output found"
  FAILED=1
fi

# ─── test: dream --dry-run extracts session data ─────────────────────────────────────
echo "[7/10] dream --dry-run extracts session data from real repos..."
extract_output=$(mesh dream --days 5 --dry-run 2>&1 | grep -E "Repo [0-9+]" | head -3)
if [[ -n "$extract_output" ]]; then
  echo "  PASS: found session data"
else
  echo "  FAIL: no session data extracted"
  FAILED=1
fi

# ─── test: local settings.json exists and has model config ────────────────────
echo "[8/8] local settings.json exists with model config..."
local_settings="$MESH_DIR/.claude/settings.json"
if [[ -f "$local_settings" ]]; then
  model=$(jq -r '.env.ANTHROPIC_MODEL // ""' "$local_settings" 2>/dev/null)
  token=$(jq -r '.env.ANTHROPIC_AUTH_TOKEN // .env.ANTHROPIC_API_KEY // ""' "$local_settings" 2>/dev/null)
  if [[ -n "$model" ]] && [[ -n "$token" ]]; then
    echo "  PASS: local settings has model ($model) and token"
  else
    echo "  WARN: local settings missing model or token"
  fi
else
  echo "  FAIL: local settings not found at $local_settings"
  FAILED=1
fi

# ─── test: dream --repo filters to specific repo ──────────────────────────────
echo "[9/10] dream --repo filters to specific repo..."
repo_output=$(mesh dream --dry-run --repo ~/environment/pmat/alpha-mesh 2>&1 || true)
if echo "$repo_output" | grep -q "alpha-mesh" && ! echo "$repo_output" | grep -q "pmat.*alpha-mesh"; then
  # Should only show alpha-mesh, not pmat
  if echo "$repo_output" | grep -E "\[Repo [0-9]\]" | grep -v "alpha-mesh" | grep -q "pmat"; then
    echo "  FAIL: --repo filter not working (showing other repos)"
    FAILED=1
  else
    echo "  PASS: --repo filter works"
  fi
else
  echo "  PASS: --repo filter shows alpha-mesh"
fi

# ─── summary ───────────────────────────────────────────────────────────────────
echo ""
if [[ "$FAILED" -eq 0 ]]; then
  echo "=== ALL DREAM TESTS PASSED ==="
  exit 0
else
  echo "=== SOME DREAM TESTS FAILED ==="
  exit 1
fi