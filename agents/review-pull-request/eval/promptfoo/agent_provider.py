"""Promptfoo Python provider (PMAT-304): review a case's commit with the real agent, return its findings.

call_api(prompt, options, context) gets the case in context["vars"]["case"] and the repeat number
in context["repeatIndex"]; the prompt is ignored. It returns {"output": review} (eval_adapter's JSON)
or {"error": ...}, which Promptfoo counts as an error, not a failure (failure class harness_error).

Run state lives in EVAL_RUN_DIR (eval/scripts/run_eval.sh sets it; gitignored):
  checkpoints-repeat-<n>.sqlite  one per repeat: cases on one commit share its review within a
                                 repeat, never across repeats; rerunning resumes a stopped review
  locks/<commit>-repeat-<n>.lock one review of a commit per repeat at a time, across processes; the
                                 repeats of a commit review at once (PMAT-328)
  traces/repeat-<n>/<commit>.jsonl  the review's trace (PMAT-325): every tool call and its result as
                                 the model got them; shared like the review, appended to on a resume
The model is EVAL_MODEL, else CODE_REVIEW_MODEL from .env. One run dir holds one model's reviews.

With EVAL_STORED_RESULTS (run_eval.sh --stored, PMAT-311) the agent does not run: each case gets its
stored review from that results.json, so the judge can score it. A hold-out case is refused. A relative trace path
(a run pulled by archive_runs.py, PMAT-336) is resolved against the folder of that results.json.
"""

import fcntl
import functools
import hashlib
import json
import os
import sqlite3
import sys
from argparse import Namespace
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from case_rules import glob_regex, holdout_ids
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
    # The cases of one commit in one repeat queue here, then reuse the first's review. run_review_agent's own run
    # lock is off (run_lock=False): it does not wait and its key has no repeat, and this lock already
    # guards what one commit's review in this repeat writes (its checkpoint thread and its trace). Other
    # commits' threads in the same store are left to SQLite's own locking (timeout=60 in _saver).
    with open(run_dir / "locks" / f"{key}-repeat-{repeat}.lock", "a+") as lock:
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


@functools.lru_cache(maxsize=1)
def _stored_rows(path: str) -> dict:
    rows = json.loads(Path(path).read_text())["results"]["results"]
    return {(r["vars"]["case_id"], r.get("repeatIndex") or 0): r for r in rows}


def _stored_review(path: str, case_id: str, repeat: int) -> dict:
    if case_id in holdout_ids():
        raise ValueError(f"{case_id} is a hold-out case: a stored run scores development cases only")
    row = _stored_rows(path).get((case_id, repeat))
    if row is None:
        raise LookupError(f"no stored review of {case_id}, repeat {repeat}, in {path}")
    response = row.get("response") or {}
    if row.get("failureReason") == 2 or response.get("output") is None:
        raise LookupError(row.get("error") or response.get("error") or "the stored run has no output")
    output = response["output"]
    output = json.loads(output) if isinstance(output, str) else output
    if output.get("trace") and not os.path.isabs(output["trace"]):  # a run pulled from the archive (PMAT-336)
        run = Path(path).resolve().parent
        trace = (run / output["trace"]).resolve()
        if not trace.is_relative_to(run):  # the judge reads the trace: never a file outside the run
            raise LookupError(f"trace {output['trace']} is outside the run folder {run}")
        output = {**output, "trace": str(trace)}
    return output


def call_api(prompt, options, context):
    case, repeat = context["vars"]["case"], context.get("repeatIndex") or 0
    stored = os.environ.get("EVAL_STORED_RESULTS")
    try:
        result = _stored_review(stored, case["case_id"], repeat) if stored else review(case, repeat)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    pattern = glob_regex(case["expected"]["flag_file"])  # a raise_nothing case's flag_file may be a glob
    unreviewed = [f for f in result["incomplete"] if pattern.match(f)]
    if unreviewed:
        return {"error": f"{', '.join(unreviewed)} not fully reviewed (unusable model response), "
                         "so this case cannot be scored"}
    return {"output": result | {"repeat": repeat}}  # so the judge can score repeat 0 only (--judge-repeats first)
