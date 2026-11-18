#!/usr/bin/env bash
# alpha-mesh/test/test-model.sh
# Tests for bin/model.sh script

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$MESH_DIR/bin:$PATH"
cd "$MESH_DIR"

FAILED=0

# model use -g writes to $HOME/.claude/settings.json, so every global run below uses a
# throwaway HOME. The real settings are checksummed now and checked on every exit path.
REAL_SETTINGS="$HOME/.claude/settings.json"
REAL_BACKUP="$REAL_SETTINGS.pre-alpha-mesh.bak"
# "none" only when the file is absent; an unreadable file is an error, never "none".
checksum() { if [[ -e "$1" ]]; then cksum < "$1"; else echo none; fi; }
# Baseline first: if it can't be read the suite stops here, before anything needs cleaning up.
settings_before="$(checksum "$REAL_SETTINGS")"
backup_before="$(checksum "$REAL_BACKUP")"
FAKE_HOME="$(mktemp -d)"

# Runs from the EXIT trap, so an abort under set -e or a Ctrl-C still checks the real files.
guard() {
  local status=$?
  rm -rf "$FAKE_HOME"
  echo "[guard] real ~/.claude/settings.json and its backup are unchanged..."
  local settings_after backup_after
  if ! settings_after="$(checksum "$REAL_SETTINGS")" || ! backup_after="$(checksum "$REAL_BACKUP")"; then
    echo "  FAIL: could not read $REAL_SETTINGS or $REAL_BACKUP to verify them"
    exit 1
  fi
  if [[ "$settings_after" != "$settings_before" || "$backup_after" != "$backup_before" ]]; then
    echo "  FAIL: a test modified $REAL_SETTINGS or $REAL_BACKUP"
    exit 1
  fi
  echo "  PASS"
  exit "$status"
}
trap guard EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "=== model tests ==="
echo ""

# ─── helpers ───────────────────────────────────────────────────────────────────
jq_ok() { jq '.' "$1" >/dev/null 2>&1; }

# ─── test: bin/model.sh exists and is executable ──────────────────────────────────
echo "[1/7] bin/model.sh exists and is executable..."
if [[ -x "$MESH_DIR/bin/model.sh" ]]; then
  echo "  PASS"
else
  echo "  FAIL: bin/model.sh not found or not executable"
  FAILED=1
fi

# ─── test: config/models.json is valid JSON ─────────────────────────────────────
echo "[2/7] config/models.json is valid JSON..."
if jq_ok "$MESH_DIR/config/models.json"; then
  echo "  PASS"
else
  echo "  FAIL: config/models.json is not valid JSON"
  FAILED=1
fi

# ─── test: required profiles exist ──────────────────────────────────────────────
echo "[3/7] Required profiles exist..."
for profile in minimax openrouter-default claude-sonnet; do
  if jq -e --arg p "$profile" '.profiles[$p]' "$MESH_DIR/config/models.json" >/dev/null 2>&1; then
    echo "  PASS: $profile exists"
  else
    echo "  FAIL: $profile not found in config/models.json"
    FAILED=1
  fi
done

# ─── test: model list outputs table ─────────────────────────────────────────────
echo "[4/7] model list outputs profiles..."
output=$(mesh model list 2>&1)
if echo "$output" | grep -q "minimax"; then
  echo "  PASS: model list shows minimax"
else
  echo "  FAIL: model list did not show minimax"
  FAILED=1
fi
if echo "$output" | grep -q "openrouter"; then
  echo "  PASS: model list shows openrouter"
else
  echo "  FAIL: model list did not show openrouter"
  FAILED=1
fi

# ─── test: model status works ───────────────────────────────────────────────────
echo "[5/7] model status works..."
if mesh model status >/dev/null 2>&1; then
  echo "  PASS"
else
  echo "  FAIL: model status returned error"
  FAILED=1
fi

# ─── test: model use -g writes to global settings and backs up ─────────────────
echo "[6/7] model use -g writes to global settings and backs up..."
global_settings="$FAKE_HOME/.claude/settings.json"
backup_file="$global_settings.pre-alpha-mesh.bak"

# Create a pre-existing settings file so there's something to back up
mkdir -p "$(dirname "$global_settings")"
echo '{"env": {"ANTHROPIC_MODEL": "old-model"}}' > "$global_settings"

HOME="$FAKE_HOME" ALPHA_MESH_MINIMAX_TOKEN="fake-test-key" mesh model use -g minimax >/dev/null 2>&1
if [[ -f "$global_settings" ]]; then
  echo "  PASS: global settings file created"
else
  echo "  FAIL: global settings file not created"
  FAILED=1
fi

if [[ -f "$backup_file" ]]; then
  echo "  PASS: backup file created with correct suffix"
else
  echo "  FAIL: backup file not created with correct suffix"
  FAILED=1
fi

# Verify content has ANTHROPIC_MODEL
if jq -e '.env.ANTHROPIC_MODEL' "$global_settings" >/dev/null 2>&1; then
  echo "  PASS: settings contains ANTHROPIC_MODEL"
else
  echo "  FAIL: settings missing ANTHROPIC_MODEL"
  FAILED=1
fi

# Verify backed up content was the old model
if [[ -f "$backup_file" ]] && jq -r '.env.ANTHROPIC_MODEL' "$backup_file" | grep -q "old-model"; then
  echo "  PASS: backup contains previous model"
else
  echo "  FAIL: backup does not contain previous model"
  FAILED=1
fi

# ─── test: model use -l writes to local settings ────────────────────────────────
echo "[7/7] model use -l writes to local settings..."
# Use a temp directory for local test — create a fake .git so no confirmation prompt
tmp_dir=$(mktemp -d)
mkdir "$tmp_dir/.git"
cd "$tmp_dir"
local_settings="$tmp_dir/.claude/settings.json"

echo "y" | ALPHA_MESH_MINIMAX_TOKEN="fake-test-key" mesh model use -l minimax >/dev/null 2>&1
if [[ -f "$local_settings" ]]; then
  echo "  PASS: local settings file created at $local_settings"
else
  echo "  FAIL: local settings file not created"
  FAILED=1
fi

# Clean up
rm -rf "$tmp_dir"
cd "$MESH_DIR"

# ─── PMAT-350: keys come only from ALPHA_MESH_<PROVIDER>_TOKEN ─────────────────
# A scratch copy of bin/ and config/ with no .env, so the developer's real .env
# cannot set the variables under test. Settings go to a scratch repo, never the real one.
SCRATCH_MESH="$(mktemp -d)"
cp -r "$MESH_DIR/bin" "$MESH_DIR/config" "$SCRATCH_MESH/"
SCRATCH_REPO="$(mktemp -d)"
mkdir "$SCRATCH_REPO/.git"
scratch_settings="$SCRATCH_REPO/.claude/settings.json"
FAKE_KEY="fake-pmat350-$RANDOM$RANDOM$$"
# Runs mesh model from the scratch copy, in the scratch repo, with no provider token set.
scratch_model() {
  (cd "$SCRATCH_REPO" && env -u ALPHA_MESH_MINIMAX_TOKEN -u ALPHA_MESH_OPENROUTER_TOKEN \
    -u ALPHA_MESH_ANTHROPIC_TOKEN HOME="$FAKE_HOME" "$@" "$SCRATCH_MESH/bin/mesh" model "${MODEL_ARGS[@]}" </dev/null)
}

echo "[8] model use -l minimax with ALPHA_MESH_MINIMAX_TOKEN unset fails and writes nothing..."
mkdir -p "$(dirname "$scratch_settings")"
echo '{"env": {"ANTHROPIC_MODEL": "untouched"}}' > "$scratch_settings"
before="$(checksum "$scratch_settings")"
MODEL_ARGS=(use -l minimax)
if out=$(scratch_model 2>&1); then
  echo "  FAIL: exited 0 with the variable unset"
  FAILED=1
elif ! grep -q "ALPHA_MESH_MINIMAX_TOKEN" <<<"$out" || ! grep -q "\.env" <<<"$out"; then
  echo "  FAIL: error does not name ALPHA_MESH_MINIMAX_TOKEN and .env: $out"
  FAILED=1
elif [[ "$(checksum "$scratch_settings")" != "$before" ]]; then
  echo "  FAIL: settings.json changed"
  FAILED=1
else
  echo "  PASS"
fi

echo "[9] model check minimax with ALPHA_MESH_MINIMAX_TOKEN unset reports the variable..."
MODEL_ARGS=(check minimax)
if out=$(scratch_model 2>&1); then
  echo "  FAIL: exited 0 with the variable unset"
  FAILED=1
elif ! grep -q "ALPHA_MESH_MINIMAX_TOKEN" <<<"$out" || ! grep -q "\.env" <<<"$out"; then
  echo "  FAIL: output does not name ALPHA_MESH_MINIMAX_TOKEN and .env: $out"
  FAILED=1
else
  echo "  PASS"
fi

echo "[10] model use -l minimax with the variable set writes it as ANTHROPIC_AUTH_TOKEN..."
MODEL_ARGS=(use -l minimax)
scratch_model ALPHA_MESH_MINIMAX_TOKEN="$FAKE_KEY" >/dev/null 2>&1 || true
if [[ "$(jq -r '.env.ANTHROPIC_AUTH_TOKEN' "$scratch_settings")" != "$FAKE_KEY" ]]; then
  echo "  FAIL: ANTHROPIC_AUTH_TOKEN is not the fake key"
  FAILED=1
elif [[ -n "$(jq -r '.env.ANTHROPIC_API_KEY // ""' "$scratch_settings")" ]]; then
  echo "  FAIL: ANTHROPIC_API_KEY is set for an auth_token profile"
  FAILED=1
elif git -C "$MESH_DIR" grep -q "$FAKE_KEY"; then
  echo "  FAIL: the fake key appears in a tracked file"
  FAILED=1
else
  echo "  PASS"
fi

echo "[11] model use -l claude-sonnet writes ANTHROPIC_API_KEY (auth: api_key)..."
MODEL_ARGS=(use -l claude-sonnet)
scratch_model ALPHA_MESH_ANTHROPIC_TOKEN="$FAKE_KEY" >/dev/null 2>&1 || true
if [[ "$(jq -r '.env.ANTHROPIC_API_KEY' "$scratch_settings")" != "$FAKE_KEY" ]]; then
  echo "  FAIL: ANTHROPIC_API_KEY is not the fake key"
  FAILED=1
elif [[ -n "$(jq -r '.env.ANTHROPIC_AUTH_TOKEN // ""' "$scratch_settings")" ]]; then
  echo "  FAIL: ANTHROPIC_AUTH_TOKEN is set for an api_key profile"
  FAILED=1
else
  echo "  PASS"
fi

rm -rf "$SCRATCH_MESH" "$SCRATCH_REPO"

# ─── summary ────────────────────────────────────────────────────────────────────
echo ""
if [[ "$FAILED" -eq 0 ]]; then
  echo "=== ALL MODEL TESTS PASSED ==="
  exit 0
else
  echo "=== SOME MODEL TESTS FAILED ==="
  exit 1
fi