#!/usr/bin/env bash
# alpha-mesh/test/test-install.sh
# Tests for install script

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$MESH_DIR"

FAILED=0

# Every install run below uses a throwaway HOME so the tests never touch the real
# ~/bin links or ~/.claude settings backup.
FAKE_HOME="$(mktemp -d)"
CREATED_TASKS=0
GITIGNORE_SNAPSHOT="$FAKE_HOME/gitignore.orig"
cleanup() {
  [[ -f "$GITIGNORE_SNAPSHOT" ]] && cp "$GITIGNORE_SNAPSHOT" "$MESH_DIR/.gitignore"
  rm -rf "$FAKE_HOME"
  [[ "$CREATED_TASKS" -eq 1 ]] && rm -rf "$MESH_DIR/tasks"
  return 0
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
# install may append to .gitignore and tests rewrite it; snapshot it (after the trap is set) so local edits are restored
[[ -f "$MESH_DIR/.gitignore" ]] && cp "$MESH_DIR/.gitignore" "$GITIGNORE_SNAPSHOT"

REAL_BACKUP="$HOME/.claude/settings.json.pre-alpha-mesh.bak"
# mtime of a file: GNU stat first, then BSD/macOS stat; "none" if the file is absent.
file_mtime() {
  [[ -e "$1" ]] || { echo none; return 0; }
  stat -c '%Y' "$1" 2>/dev/null || stat -f '%m' "$1" 2>/dev/null || echo unsupported
}
backup_before="$(file_mtime "$REAL_BACKUP")"

run_install() { echo 'n' | HOME="$FAKE_HOME" "$MESH_DIR/bin/install.sh" 2>&1; }

echo "=== install tests ==="
echo ""

# ─── test: bin/install.sh exists and is executable ────────────────────────────────
echo "[1/11] bin/install.sh exists and is executable..."
if [[ -x "$MESH_DIR/bin/install.sh" ]]; then
  echo "  PASS"
else
  echo "  FAIL: bin/install.sh not found or not executable"
  FAILED=1
fi

# ─── test: required tools are available ─────────────────────────────────────────
echo "[2/11] Required tools are available..."
for tool in jq sqlite3 curl python3; do
  if command -v "$tool" >/dev/null 2>&1; then
    echo "  PASS: $tool found"
  else
    echo "  FAIL: $tool not found in PATH"
    FAILED=1
  fi
done

# ─── test: bin/install.sh can be run (dry-run mode if available) ───────────────────
echo "[3/11] bin/install.sh runs without error..."
# Run install and capture output — may need to handle interactive prompts
# Use expect-style timeout or check for non-interactive mode
output=$(run_install || true)
if echo "$output" | grep -qi "error\|fail"; then
  # Check if it's a real error or just the "not running as root" early exit
  if echo "$output" | grep -qi "sudo\|root\|permission\|denied"; then
    echo "  SKIP: install requires elevated permissions (expected in dev env)"
  else
    echo "  FAIL: install script reported an error"
    echo "  Output: $output"
    FAILED=1
  fi
else
  echo "  PASS: install script runs without error"
fi

# ─── test: install keeps .claude/ and .mesh/ ignored ───────────────────────────
# bin/install.sh rewrites .gitignore; it must not drop the runtime-dir entries.
echo "[4/11] .gitignore still ignores .claude/ and .mesh/ after install..."
# The assertions are only meaningful if install got as far as writing .gitignore.
if ! echo "$output" | grep -q "✓ .gitignore"; then
  echo "  FAIL: install did not reach the .gitignore step (output above is from test 3)"
  FAILED=1
fi
for d in .claude/ .mesh/ 'tasks/*.db'; do
  # Check the rewritten file itself: git check-ignore would also honour a global gitignore.
  if grep -qxF "$d" "$MESH_DIR/.gitignore"; then
    echo "  PASS: $d listed in .gitignore"
  else
    echo "  FAIL: $d missing from .gitignore after install"
    FAILED=1
  fi
done

# ─── test: install leaves a complete .gitignore exactly as it is ────────────────
# PMAT-352: install used to overwrite .gitignore with a stale template, dropping committed lines.
echo "[5/11] install leaves the committed .gitignore byte-identical..."
if [[ ! -f "$GITIGNORE_SNAPSHOT" ]]; then
  echo "  FAIL: no committed .gitignore to compare against"
  FAILED=1
else
  cp "$GITIGNORE_SNAPSHOT" "$MESH_DIR/.gitignore"
  run_install >/dev/null || true
  if cmp -s "$GITIGNORE_SNAPSHOT" "$MESH_DIR/.gitignore"; then
    echo "  PASS"
  else
    echo "  FAIL: install changed the committed .gitignore:"
    diff "$GITIGNORE_SNAPSHOT" "$MESH_DIR/.gitignore" | head -n 20 || true
    FAILED=1
  fi
fi

# ─── test: install only appends missing entries, keeping every other line ────────
echo "[6/11] install keeps unknown .gitignore lines and adds a missing entry once..."
partial="$FAKE_HOME/gitignore.partial"
printf '%s\n' '# local comment' 'keep-me-PMAT-352/' '.claude/' > "$partial"   # no .mesh/
cp "$partial" "$MESH_DIR/.gitignore"
run_install >/dev/null || true
first="$(cat "$MESH_DIR/.gitignore")"
run_install >/dev/null || true
if [[ "$(head -n 3 "$MESH_DIR/.gitignore")" != "$(cat "$partial")" ]]; then
  echo "  FAIL: install did not keep the original lines in order at the top"
  FAILED=1
elif [[ "$(grep -cxF '.mesh/' "$MESH_DIR/.gitignore")" != 1 ]]; then
  echo "  FAIL: .mesh/ should appear exactly once"
  FAILED=1
elif [[ "$(grep -cxF '.claude/' "$MESH_DIR/.gitignore")" != 1 ]]; then
  echo "  FAIL: .claude/ was already there and should not be added again"
  FAILED=1
elif [[ "$(cat "$MESH_DIR/.gitignore")" != "$first" ]]; then
  echo "  FAIL: a second install changed .gitignore again"
  FAILED=1
else
  echo "  PASS"
fi
[[ -f "$GITIGNORE_SNAPSHOT" ]] && cp "$GITIGNORE_SNAPSHOT" "$MESH_DIR/.gitignore"

# ─── test: install does not create the removed tasks/ directory ────────────────
echo "[7/11] install does not create tasks/ (task-log was removed)..."
if [[ -e "$MESH_DIR/tasks" ]]; then
  echo "  SKIP: tasks/ already exists in this checkout (see the legacy-data check below)"
else
  run_install >/dev/null || true
  if [[ -e "$MESH_DIR/tasks" ]]; then
    echo "  FAIL: install created tasks/"
    rmdir "$MESH_DIR/tasks" 2>/dev/null || true
    FAILED=1
  else
    echo "  PASS"
  fi
fi

# ─── test: install warns about leftover task-log data instead of deleting it ────
echo "[8/11] install warns about legacy tasks/ data and keeps it..."
if [[ -e "$MESH_DIR/tasks" ]]; then
  echo "  SKIP: tasks/ already exists in this checkout"
else
  CREATED_TASKS=1
  mkdir -p "$MESH_DIR/tasks" && : > "$MESH_DIR/tasks/task-log.db"
  legacy_out=$(run_install || true)
  if ! echo "$legacy_out" | grep -qi "legacy tasks/"; then
    echo "  FAIL: install did not mention the leftover tasks/ directory"
    FAILED=1
  elif [[ ! -e "$MESH_DIR/tasks/task-log.db" ]]; then
    echo "  FAIL: install deleted user data in tasks/"
    FAILED=1
  else
    echo "  PASS"
  fi
  rm -rf "$MESH_DIR/tasks"
  CREATED_TASKS=0
fi

# ─── test: install removes dangling links to retired commands ───────────────────
echo "[9/11] install removes dangling ~/bin links that point into alpha-mesh..."
mkdir -p "$FAKE_HOME/bin"
ln -s "$MESH_DIR/bin/retired-command" "$FAKE_HOME/bin/retired-command"   # dangling, ours
ln -s "../$(basename "$MESH_DIR")/bin/retired-relative" "$FAKE_HOME/bin/retired-relative"   # relative form
ln -s /nonexistent/other-tool "$FAKE_HOME/bin/other-tool"                # dangling, not ours
run_install >/dev/null || true
# the relative link resolves from $FAKE_HOME/bin; give it a real target dir so it points into the repo
mkdir -p "$FAKE_HOME/$(basename "$MESH_DIR")"
ln -sfn "$MESH_DIR/bin" "$FAKE_HOME/$(basename "$MESH_DIR")/bin"
run_install >/dev/null || true
if [[ -L "$FAKE_HOME/bin/retired-relative" ]]; then
  echo "  FAIL: dangling RELATIVE link into alpha-mesh was left behind"
  FAILED=1
elif [[ -L "$FAKE_HOME/bin/retired-command" ]]; then
  echo "  FAIL: dangling link to a retired alpha-mesh command was left behind"
  FAILED=1
elif [[ ! -L "$FAKE_HOME/bin/other-tool" ]]; then
  echo "  FAIL: install removed a link that does not belong to alpha-mesh"
  FAILED=1
else
  echo "  PASS"
fi

# ─── test: install links only mesh, replacing the per-command links ─────────────
# Before PMAT-348 every bin/ entry was linked into ~/bin under its own name;
# mesh <command> is now the one way in, so install leaves only ~/bin/mesh.
echo "[10/11] install links only mesh into ~/bin and removes the old per-command links..."
rm -rf "$FAKE_HOME/bin" && mkdir -p "$FAKE_HOME/bin"
for old in model install dream review-pull-request; do ln -s "$MESH_DIR/bin/$old" "$FAKE_HOME/bin/$old"; done
ln -s "$MESH_DIR/bin/lib" "$FAKE_HOME/bin/lib"   # still a valid target: not dangling
ln -s /nonexistent/other-tool "$FAKE_HOME/bin/other-tool"
run_install >/dev/null || true
ours=$(for l in "$FAKE_HOME/bin/"*; do
  [[ -L "$l" ]] || continue
  t="$(readlink -f "$l" 2>/dev/null || readlink "$l")"
  if [[ "$t" == "$MESH_DIR/"* || "$t" == "$(readlink -f "$MESH_DIR")/"* ]]; then basename "$l"; fi
done | tr '\n' ' ')
if [[ "$(readlink -f "$FAKE_HOME/bin/mesh" 2>/dev/null)" != "$(readlink -f "$MESH_DIR/bin/mesh")" ]]; then
  echo "  FAIL: ~/bin/mesh does not point at bin/mesh"
  FAILED=1
elif [[ "$ours" != "mesh " ]]; then
  echo "  FAIL: ~/bin links into alpha-mesh are '$ours', expected only 'mesh'"
  FAILED=1
elif [[ ! -L "$FAKE_HOME/bin/other-tool" ]]; then
  echo "  FAIL: install removed a link that does not belong to alpha-mesh"
  FAILED=1
else
  echo "  PASS"
fi

# ─── test: the suite left the real HOME alone ───────────────────────────────────
echo "[11/11] tests did not touch the real ~/.claude settings backup..."
backup_after="$(file_mtime "$REAL_BACKUP")"
if [[ "$backup_before" == "unsupported" ]]; then
  echo "  FAIL: cannot read file mtimes with this platform's stat, so the check proves nothing"
  FAILED=1
elif [[ "$backup_before" == "$backup_after" ]]; then
  echo "  PASS"
else
  echo "  FAIL: install modified $REAL_BACKUP"
  FAILED=1
fi

# ─── summary ────────────────────────────────────────────────────────────────────
echo ""
if [[ "$FAILED" -eq 0 ]]; then
  echo "=== ALL INSTALL TESTS PASSED ==="
  exit 0
else
  echo "=== SOME INSTALL TESTS FAILED ==="
  exit 1
fi