#!/usr/bin/env bash
# alpha-mesh/test/test-orchestrator.sh
# Runs the orchestrator pytest suite (fake tmux, gh and Jira) and checks the mesh entry point.

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_DIR="$MESH_DIR/agents/orchestrator"

echo "=== orchestrator tests ==="
if (cd "$AGENT_DIR" && uv run pytest -q); then
  echo "  PASS: pytest suite"
else
  echo "  FAIL: pytest suite"
  exit 1
fi

if "$MESH_DIR/bin/mesh" orchestrate --help | grep -q "mesh orchestrate"; then
  echo "  PASS: mesh orchestrate --help"
else
  echo "  FAIL: mesh orchestrate --help"
  exit 1
fi
