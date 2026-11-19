#!/usr/bin/env bash
# alpha-mesh/test/test-config-no-keys.sh
# Guard (PMAT-350): no key reaches a file under config/. Keys live only in .env (gitignored).
# Fails if any file under config/ has a field whose name ends in TOKEN or KEY, or a value
# that looks like a key (sk-..., or a long random-looking string).

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FAILED=0

# Prints each problem found under $1; returns 1 if there is any.
scan_dir() {
  local dir="$1" found=0 file hit
  while IFS= read -r -d '' file; do
    # A field name ending in TOKEN or KEY, followed by : (JSON/YAML) or = (env/ini)
    while IFS= read -r hit; do
      echo "  $file: key-like field name: $hit"
      found=1
    done < <(grep -noiE '"?[A-Za-z0-9_.-]*(token|key)"?[[:space:]]*[:=]' "$file" || true)
    # sk-... keys
    while IFS= read -r hit; do
      echo "  $file: sk- value"
      found=1
    done < <(grep -noE 'sk-[A-Za-z0-9_-]{8,}' "$file" || true)
    # A run of 32+ key characters mixing letters and digits (URLs and model names break on / . :)
    while IFS= read -r hit; do
      if [[ "${hit#*:}" =~ [0-9] && "${hit#*:}" =~ [A-Za-z] ]]; then
        echo "  $file:${hit%%:*}: random-looking value"
        found=1
      fi
    done < <(grep -noE '[A-Za-z0-9_+=-]{32,}' "$file" || true)
  done < <(find "$dir" -type f -print0)
  return "$found"
}

echo "=== config no-keys guard ==="
echo ""

echo "[1/3] config/ holds no key-like field or value..."
if scan_dir "$MESH_DIR/config"; then
  echo "  PASS"
else
  echo "  FAIL: remove keys from config/ — keys go in .env only"
  FAILED=1
fi

# The guard can fail: plant each kind of key in a scratch copy of config/
scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT

echo "[2/3] guard fails on a planted sk- value..."
cp -r "$MESH_DIR/config" "$scratch/sk"
jq '.profiles.minimax.description = "sk-planted0123456789abcdef"' "$MESH_DIR/config/models.json" > "$scratch/sk/models.json"
if scan_dir "$scratch/sk" >/dev/null; then
  echo "  FAIL: guard passed with an sk- value planted"
  FAILED=1
else
  echo "  PASS"
fi

echo "[3/3] guard fails on a planted ANTHROPIC_AUTH_TOKEN field..."
cp -r "$MESH_DIR/config" "$scratch/field"
jq '.profiles.minimax.env.ANTHROPIC_AUTH_TOKEN = "x"' "$MESH_DIR/config/models.json" > "$scratch/field/models.json"
if scan_dir "$scratch/field" >/dev/null; then
  echo "  FAIL: guard passed with an ANTHROPIC_AUTH_TOKEN field planted"
  FAILED=1
else
  echo "  PASS"
fi

echo ""
if [[ "$FAILED" -eq 0 ]]; then
  echo "=== CONFIG NO-KEYS GUARD PASSED ==="
  exit 0
else
  echo "=== CONFIG NO-KEYS GUARD FAILED ==="
  exit 1
fi
