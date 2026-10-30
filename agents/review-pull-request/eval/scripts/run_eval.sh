#!/usr/bin/env bash
# Run the evals (PMAT-304): every case in eval/cases, reviewed at its raised_sha, k times.
#
#   eval/scripts/run_eval.sh [--model <model>] [--k <n>] [--run <name>] [promptfoo eval args...]
#
# Rerunning with the same --run resumes: finished reviews are reused, stopped ones continue from the
# last finished file. Writes eval/runs/<name>/ (gitignored): results.json, run.json, checkpoints.
# Then: uv run python eval/scripts/baseline.py eval/runs/<name>   (or compare_baseline.py <results.json>)
set -euo pipefail

EVAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT="$(dirname "$EVAL")"
MODEL="" K=3 RUN=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --k) K="$2"; shift 2 ;;
    --run) RUN="$2"; shift 2 ;;
    *) break ;;
  esac
done
[[ -n "$MODEL" ]] || MODEL="$(grep -E '^CODE_REVIEW_MODEL=' "$AGENT/../../.env" | cut -d= -f2- | tr -d '"')"
[[ -n "$MODEL" ]] || { echo "ERROR: no model: pass --model or set CODE_REVIEW_MODEL in .env" >&2; exit 1; }
RUN="${RUN:-$(date +%Y%m%d-%H%M)-${MODEL//[^A-Za-z0-9.-]/_}}"
RUN_DIR="$EVAL/runs/$RUN"
mkdir -p "$RUN_DIR"

# promptfoo needs Node >= 22.22; the machine default stays as it is outside this script.
# shellcheck disable=SC1091
source "${NVM_DIR:-$HOME/.nvm}/nvm.sh" >/dev/null
nvm use 24 >/dev/null

COMMIT="$(git -C "$AGENT" rev-parse HEAD)"
DIRTY=false
if [[ -n "$(git -C "$AGENT" status --porcelain -- src eval/promptfoo eval/cases)" ]]; then
  DIRTY=true
  echo "WARNING: uncommitted changes in src/, eval/promptfoo/ or eval/cases/: this run cannot become the baseline" >&2
fi
# run.json: what baseline.py records; a resumed run must keep its model (the checkpoint key has none).
"$AGENT/.venv/bin/python" - "$RUN_DIR/run.json" "$COMMIT" "$MODEL" "$(date +%Y-%m-%d)" "$K" "$DIRTY" <<'PY'
import json, sys
from pathlib import Path
path, commit, model, date, k, dirty = sys.argv[1:]
path = Path(path)
if path.exists() and json.loads(path.read_text())["model"] != model:
    sys.exit(f"ERROR: {path.parent} holds another model's reviews; use a new --run")
path.write_text(json.dumps({"commit": commit, "model": model, "date": date, "k": int(k), "window": 3,
                            "dirty": dirty == "true"}) + "\n")
PY

echo "Run: $RUN_DIR (model $MODEL, k=$K, commit ${COMMIT:0:7})" >&2
cd "$EVAL"
EVAL_RUN_DIR="$RUN_DIR" EVAL_MODEL="$MODEL" PROMPTFOO_PYTHON="$AGENT/.venv/bin/python" \
  PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_DISABLE_UPDATE=1 PROMPTFOO_CONFIG_DIR="$RUN_DIR/.promptfoo" \
  promptfoo eval -c promptfooconfig.yaml --repeat "$K" --no-cache -o "$RUN_DIR/results.json" "$@"
