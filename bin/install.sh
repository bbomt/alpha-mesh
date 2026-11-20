#!/usr/bin/env bash
# alpha-mesh/bin/install.sh
# Run once after cloning alpha-mesh.

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== PMAT alpha-mesh install ==="
echo "Mesh dir: $MESH_DIR"
echo ""

# ─── Check dependencies ─────────────────────────────────────────────────────
echo "Checking dependencies..."

missing=()
for cmd in jq sqlite3 curl python3; do
  if command -v "$cmd" &>/dev/null; then
    echo "  ✓ $cmd"
  else
    echo "  ✗ $cmd  ← MISSING"
    missing+=("$cmd")
  fi
done

if [[ ${#missing[@]} -gt 0 ]]; then
  echo ""
  echo "Install missing dependencies:"
  echo "  sudo apt-get install ${missing[*]}"
  exit 1
fi

echo ""

# ─── Make scripts executable ────────────────────────────────────────────────
echo "Setting script permissions..."
chmod +x "$MESH_DIR/bin/"*
echo "  ✓ done"
echo ""

# ─── Create directory structure ─────────────────────────────────────────────
echo "Creating directories..."
mkdir -p "$MESH_DIR/config"
echo "  ✓ done"
echo ""

# ─── Ensure .gitignore has the entries install needs ────────────────────────
# Append only missing entries: never rewrite the committed .gitignore (PMAT-352).
GITIGNORE_ENTRIES=(
  config/active.json            # secrets — never commit
  'tasks/*.db'                  # legacy task-log data (feature removed)
  __pycache__/ '*.pyc' '*.pyo'  # Python bytecode
  venv/ 'agents/*/*.egg-info/' 'agents/*/.venv/' 'agents/*/uv.lock' uv.lock
  .DS_Store '*.swp' .env
  .claude/ .mesh/               # runtime dirs: settings.json can hold tokens; MEMORY.md holds session text
)
added=0
for entry in "${GITIGNORE_ENTRIES[@]}"; do
  if ! grep -qxF -- "$entry" "$MESH_DIR/.gitignore" 2>/dev/null; then
    # start on a new line if the file does not end with one
    if [[ -s "$MESH_DIR/.gitignore" && -n "$(tail -c 1 "$MESH_DIR/.gitignore")" ]]; then
      echo >> "$MESH_DIR/.gitignore"
    fi
    echo "$entry" >> "$MESH_DIR/.gitignore"
    added=$((added + 1))
  fi
done
echo "  ✓ .gitignore checked ($added entries added)"
echo ""

# ─── Add scripts to PATH via shell profile ──────────────────────────────────
SHELL_PROFILE=""
if [[ -f "$HOME/.bashrc" ]]; then
  SHELL_PROFILE="$HOME/.bashrc"
elif [[ -f "$HOME/.bash_profile" ]]; then
  SHELL_PROFILE="$HOME/.bash_profile"
elif [[ -f "$HOME/.zshrc" ]]; then
  SHELL_PROFILE="$HOME/.zshrc"
fi

# ─── Symlink scripts into ~/bin for portable PATH-free access ──────────────────
echo "Creating symlinks in ~/bin..."
mkdir -p "$HOME/bin"

# The task-log feature was removed; its local data is never deleted automatically.
if [[ -d "$MESH_DIR/tasks" ]]; then
  echo "  NOTE: legacy tasks/ directory found (old task-log data); the task-log feature was removed."
  echo "        tasks/*.db stays git-ignored, but nothing uses this folder now. Back it up or delete it: $MESH_DIR/tasks"
fi

# mesh <command> is the one way in, so ~/bin holds only mesh. Remove every other link into
# this repo first (the per-command links older installs made, and dangling ones to retired
# commands), then link mesh. Only links whose target lives inside this repo are touched.
for link in "$HOME/bin/"*; do
  [[ -L "$link" ]] || continue
  # Resolve relative targets too (readlink -f works for dangling links).
  target="$(readlink -f "$link" 2>/dev/null || readlink "$link")"
  if [[ "$target" == "$MESH_DIR/"* || "$target" == "$(readlink -f "$MESH_DIR")/"* ]]; then
    rm -f "$link"
    [[ "$(basename "$link")" == mesh ]] || echo "  Removing: $link"
  fi
done

ln -sfn "$MESH_DIR/bin/mesh" "$HOME/bin/mesh"
echo "  ✓ mesh"
echo ""

# ─── Prompt for API keys ────────────────────────────────────────────────────
echo "=== API Key Setup ==="
echo "Copy .env.example to .env and add your tokens:"
echo "  cp .env.example .env"
echo "  \$EDITOR .env"
echo ""
echo "Tokens needed:"
echo "  ALPHA_MESH_MINIMAX_TOKEN    → your Minimax sk-cp-... key"
echo "  ALPHA_MESH_OPENROUTER_TOKEN  → your OpenRouter SK/... key"
echo "  ALPHA_MESH_ANTHROPIC_TOKEN   → your Anthropic key (optional)"
echo "Keys go in .env only — never in config/models.json."
echo ""

# ─── Backup existing settings.json ──────────────────────────────────────────
CLAUDE_SETTINGS="$HOME/.claude/settings.json"
if [[ -f "$CLAUDE_SETTINGS" ]]; then
  cp "$CLAUDE_SETTINGS" "$CLAUDE_SETTINGS.pre-alpha-mesh.bak"
  echo "  ✓ Backed up existing settings.json → settings.json.pre-alpha-mesh.bak"
  echo "    (alpha-mesh/bin/model.sh will manage this file from now on)"
  echo ""
fi

# ─── Done ───────────────────────────────────────────────────────────────────
echo "=== Install complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env — add your API tokens"
echo "  2. mesh model use minimax          # verify model switching works"
echo "  3. mesh dream --dry-run            # test extraction without writing"
echo "  4. At end of each work session: mesh dream"
echo ""
echo "Daily workflow:"
echo "  mesh model status                  # check active model"
echo "  ... do work in Claude Code ..."
echo "  mesh dream                         # consolidate sessions into .mesh/MEMORY.md"
