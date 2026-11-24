#!/usr/bin/env bash
# Run the evals (PMAT-304): every development case in eval/cases, reviewed at its raised_sha, k times, on the
# model --model names, else [model] review in review_config.toml (the model the reviews use).
#
#   eval/scripts/run_eval.sh [--model <model>] [--k <n>] [--run <name>] [--judge-repeats first|all] [--no-judge-cache] [--holdout] [promptfoo eval args...]
#   eval/scripts/run_eval.sh --stored <run dir> [--k <n>] [--run <name>] [--judge-repeats first|all] [--no-judge-cache] [promptfoo eval args...]
#
# --judge-repeats: the judge scores repeat 0 of each case only (first, the default: while iterating on the agent)
# or all k repeats (all: before merging an agent change). The line checks run on every repeat either way.
# --no-judge-cache: the judge calls its model for every row, even one whose exact request it judged before (the
# judgment cache in ~/.mesh/judge-cache/, PMAT-331), and stores nothing: e.g. to measure the judge's own agreement.
# --stored runs the judge on that run's stored reviews without running the agent (PMAT-311). Its results go to a
# new run folder with no run.json, so they can never become the baseline.
# --holdout runs the hold-out cases instead, into results-holdout.json, and prints only their totals. Use it
# only when deciding whether to ship an agent change (eval/README.md).
#
# Rerunning with the same --run resumes: finished reviews are reused, stopped ones continue from the
# last finished file. Writes eval/runs/<name>/ (gitignored): results.json, run.json, checkpoints.
# Then: uv run python eval/scripts/write_baseline.py eval/runs/<name>   (or compare_baseline.py <results.json>)
set -euo pipefail

EVAL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT="$(dirname "$EVAL")"
MODEL="" K="" RUN="" HOLDOUT="" STORED="" JUDGE_REPEATS="first" JUDGE_CACHE="on"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --model) MODEL="$2"; shift 2 ;;
    --k) K="$2"; shift 2 ;;
    --run) RUN="$2"; shift 2 ;;
    --holdout) HOLDOUT=1; shift ;;
    --stored) STORED="$2"; shift 2 ;;
    --judge-repeats) JUDGE_REPEATS="$2"; shift 2 ;;
    --no-judge-cache) JUDGE_CACHE="off"; shift ;;
    *) break ;;
  esac
done
[[ "$JUDGE_REPEATS" == first || "$JUDGE_REPEATS" == all ]] || { echo "ERROR: --judge-repeats is first or all, not '$JUDGE_REPEATS'" >&2; exit 1; }
if [[ -n "$STORED" ]]; then
  [[ -z "$HOLDOUT" ]] || { echo "ERROR: --stored scores development cases only: it cannot run with --holdout" >&2; exit 1; }
  [[ -f "$STORED/results.json" && -f "$STORED/run.json" ]] || { echo "ERROR: $STORED has no results.json and run.json" >&2; exit 1; }
  STORED="$(cd "$STORED" && pwd)"
  MODEL="$("$AGENT/.venv/bin/python" -c 'import json, sys; print(json.load(open(sys.argv[1]))["model"])' "$STORED/run.json")"
  [[ -n "$K" ]] || K="$("$AGENT/.venv/bin/python" -c 'import json, sys; print(json.load(open(sys.argv[1]))["k"])' "$STORED/run.json")"
  RUN="${RUN:-judge-$(basename "$STORED")-$(date +%Y%m%d-%H%M)}"
fi
K="${K:-3}"
# The default is the agent's own model ([model] review in review_config.toml): the eval measures what the reviews run.
[[ -n "$MODEL" ]] || MODEL="$("$AGENT/.venv/bin/python" -c 'from review_pull_request.config import configured_model; print(configured_model("review"))')"
RUN="${RUN:-$(date +%Y%m%d-%H%M)-${MODEL//[^A-Za-z0-9.-]/_}}"
RUN_DIR="$EVAL/runs/$RUN"
mkdir -p "$RUN_DIR"

# promptfoo needs Node >= 22.22; the machine default stays as it is outside this script.
# shellcheck disable=SC1091
source "${NVM_DIR:-$HOME/.nvm}/nvm.sh" >/dev/null
nvm use 24 >/dev/null

COMMIT="$(git -C "$AGENT" rev-parse HEAD)"
if [[ -z "$STORED" ]]; then
  DIRTY=false
  if [[ -n "$(git -C "$AGENT" status --porcelain -- src eval/promptfoo eval/cases)" ]]; then
    DIRTY=true
    echo "WARNING: uncommitted changes in src/, eval/promptfoo/ or eval/cases/: this run cannot become the baseline" >&2
  fi
  # run.json: what write_baseline.py records; a resumed run keeps its commit, model and k (write_eval_run_info.py).
  "$AGENT/.venv/bin/python" "$EVAL/scripts/write_eval_run_info.py" "$RUN_DIR/run.json" "$COMMIT" "$MODEL" "$(date +%Y-%m-%d)" "$K" "$DIRTY"
fi

echo "Run: $RUN_DIR (model $MODEL, k=$K, judge repeats $JUDGE_REPEATS, judge cache $JUDGE_CACHE, commit ${COMMIT:0:7}${STORED:+, stored reviews from $STORED})" >&2
cd "$EVAL"
RESULTS="$RUN_DIR/results.json"
if [[ -n "$HOLDOUT" ]]; then
  RESULTS="$RUN_DIR/results-holdout.json"  # apart, so write_baseline.py and compare_baseline.py never read it
  set -- --no-table "$@"                   # Promptfoo's table would show the failures
fi
STATUS=0
# Templating off: Promptfoo renders string vars as Nunjucks, which can run code; the provider ignores the prompt.
EVAL_RUN_DIR="$RUN_DIR" EVAL_JUDGE_REPEATS="$JUDGE_REPEATS" EVAL_JUDGE_CACHE="$JUDGE_CACHE" EVAL_MODEL="$MODEL" EVAL_HOLDOUT="$HOLDOUT" EVAL_STORED_RESULTS="${STORED:+$STORED/results.json}" PROMPTFOO_PYTHON="$AGENT/.venv/bin/python" \
  PROMPTFOO_DISABLE_TEMPLATING=true PROMPTFOO_DISABLE_TELEMETRY=1 PROMPTFOO_DISABLE_UPDATE=1 PROMPTFOO_CONFIG_DIR="$RUN_DIR/.promptfoo" \
  promptfoo eval -c promptfooconfig.yaml --repeat "$K" --no-cache -o "$RESULTS" "$@" || STATUS=$?
if [[ -n "$HOLDOUT" ]]; then
  "$AGENT/.venv/bin/python" "$EVAL/scripts/holdout_report.py" "$RESULTS"
fi
exit "$STATUS"
