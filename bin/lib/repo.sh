#!/usr/bin/env bash
# alpha-mesh/bin/lib/repo.sh
# Shared library for repo navigation and model reading.
# Source this from other scripts: source "$(dirname "${BASH_SOURCE[0]}")/lib/repo.sh"

# Find the root of the current git repository by walking up from CWD.
# Returns the repo root path, or "/" if no .git directory was found.
find_repo_root() {
  local repo_root="$PWD"
  while [[ "$repo_root" != "/" ]]; do
    if [[ -d "$repo_root/.git" ]]; then
      break
    fi
    repo_root="$(dirname "$repo_root")"
  done
  echo "$repo_root"
}

# Read the active model from local (repo-level) settings, falling back to global.
# Local settings take precedence. Returns "unknown" if neither exists.
get_active_model_from_settings() {
  local repo_root
  repo_root=$(find_repo_root)

  local local_settings="$repo_root/.claude/settings.json"
  local global_settings="$HOME/.claude/settings.json"

  local model=""
  if [[ -f "$local_settings" ]]; then
    model=$(jq -r '.env.ANTHROPIC_MODEL // .env.ANTHROPIC_DEFAULT_SONNET_MODEL // ""' "$local_settings" 2>/dev/null || echo "")
  fi
  if [[ -z "$model" || "$model" == "null" ]] && [[ -f "$global_settings" ]]; then
    model=$(jq -r '.env.ANTHROPIC_MODEL // .env.ANTHROPIC_DEFAULT_SONNET_MODEL // ""' "$global_settings" 2>/dev/null || echo "")
  fi

  echo "${model:-unknown}"
}