#!/usr/bin/env bash
# alpha-mesh/bin/orchestrate.sh
# Thin proxy → inner bash entry point.

set -euo pipefail

SCRIPT_REALPATH="$(readlink -f "${BASH_SOURCE[0]}")"
MESH_DIR="$(cd "$(dirname "$SCRIPT_REALPATH")/.." && pwd)"
INNER_BIN="$MESH_DIR/agents/orchestrator/bin/orchestrate.sh"

exec bash "$INNER_BIN" "$@"
