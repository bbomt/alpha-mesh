"""Promptfoo Python provider (PMAT-304): review a case's commit with the real agent, return its findings.

call_api(prompt, options, context) gets the case in context["vars"]["case"] and the repeat number
in context["repeatIndex"]; the prompt is ignored. It returns {"output": review} (eval_adapter's JSON)
or {"error": ...}, which Promptfoo counts as an error, not a failure (failure class harness_error).

Run state lives in EVAL_RUN_DIR (eval/scripts/run_eval.sh sets it; gitignored):
  checkpoints-repeat-<n>.sqlite  one per repeat: cases on one commit share its review within a
                                 repeat, never across repeats; rerunning resumes a stopped review
  locks/<commit>.lock            one review of a commit at a time, across repeats and processes
  traces/repeat-<n>/<commit>.jsonl  the review's trace (PMAT-325): every tool call and its result as
                                 the model got them; shared like the review, appended to on a resume
The model is EVAL_MODEL, else CODE_REVIEW_MODEL from .env. One run dir holds one model's reviews.
"""

import fcntl
import hashlib
import os
import sqlite3
import sys
from argparse import Namespace
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from case_rules import glob_regex
from eval_adapter import review_case
from review_pull_request.config import load_config
from review_pull_request.git import get_pr_description
from review_pull_request.github import _get_github_token

_savers: dict[Path, SqliteSaver] = {}


def _run_dir() -> Path:
    if not os.environ.get("EVAL_RUN_DIR"):
        raise RuntimeError("EVAL_RUN_DIR is not set: run the eval with eval/scripts/run_eval.sh")
    return Path(os.environ["EVAL_RUN_DIR"])


def _saver(path: Path) -> SqliteSaver:
    """The eval's own checkpoint store, never the production DynamoDB one: a review here must not
    touch (or, on a rerun, delete) the checkpoint of the real PR review at the same commit."""
    if path not in _savers:
        path.parent.mkdir(parents=True, exist_ok=True)
        _savers[path] = SqliteSaver(sqlite3.connect(path, check_same_thread=False, timeout=60))
    return _savers[path]


def review(case: dict, repeat: int) -> dict:
    run_dir = _run_dir()
    key = hashlib.sha256(f"{case['repo']}@{case['raised_sha']}".encode()).hexdigest()[:16]
    (run_dir / "locks").mkdir(parents=True, exist_ok=True)
    # The core's own run lock does not wait (and its key has no repeat), so the cases of one commit queue here.
    with open(run_dir / "locks" / f"{key}.lock", "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            config = load_config(Namespace(model=os.environ.get("EVAL_MODEL"), base=None, config=None,
                                           repo_path=None, dry_run=False, verbose=False, hide_pr_comment=False))
        except SystemExit as e:  # load_config exits on a missing .env value; its message is on stderr
            raise RuntimeError(f"cannot load the review config (exit {e.code}): see stderr") from e
        token = _get_github_token() or ""
        if not token:
            raise RuntimeError("no GitHub token: run 'gh auth login' (or set GITHUB_TOKEN)")
        # A fixture commit has no PR and states its own. A PR's is today's: it may have been edited since
        # raised_sha (README, Known limitations).
        description = case.get("description")
        if description is None:
            description = get_pr_description(f"https://github.com/{case['repo']}/pull/{case['pr_number']}", token)
        print(f"[eval] {case['case_id']}: {case['repo']}@{case['raised_sha'][:7]} repeat {repeat}", file=sys.stderr)
        return review_case(case, config=config, token=token, description=description,
                           checkpointer=_saver(run_dir / f"checkpoints-repeat-{repeat}.sqlite"),
                           # resolved: the output names the trace, and a run folder linked from a worktree
                           # outlives the worktree
                           trace_path=run_dir.resolve() / "traces" / f"repeat-{repeat}" / f"{key}.jsonl")


def call_api(prompt, options, context):
    case, repeat = context["vars"]["case"], context.get("repeatIndex") or 0
    try:
        result = review(case, repeat)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    pattern = glob_regex(case["expected"]["flag_file"])  # a raise_nothing case's flag_file may be a glob
    unreviewed = [f for f in result["incomplete"] if pattern.match(f)]
    if unreviewed:
        return {"error": f"{', '.join(unreviewed)} not fully reviewed (unusable model response), "
                         "so this case cannot be scored"}
    return {"output": result}
