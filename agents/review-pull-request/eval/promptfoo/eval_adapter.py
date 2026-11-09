"""The eval adapter (PMAT-304): a case -> the review core's inputs -> the review's findings.

`mesh review-pull-request` (the PR adapter) and this adapter call the same core, review_commit,
with (owner, repo, base, head, description): here the case's base_sha, its raised_sha and the PR's
description. Nothing is posted. The findings come from the review's final graph state (the data the
report is rendered from), read back from the checkpointer this adapter passes in. The review's trace
(PMAT-325) goes to trace_path; the output names it and takes files_read from it.
"""

from pathlib import Path

from review_pull_request.checkpoints import thread_id
from review_pull_request.graph import build_review_graph
from review_pull_request.review import review_commit
from review_pull_request.trace import load as load_trace


class ReviewFailed(Exception):
    """The review left no finished result (model unusable, GitHub error...)."""


def review_case(case: dict, *, config, token: str, description: str, checkpointer, trace_path: Path) -> dict:
    """Review the case's commit, or reuse the finished review of it in this checkpointer."""
    owner, repo = case["repo"].split("/")
    head = case["raised_sha"]
    thread = thread_id(owner, repo, case.get("pr_number"), head, "full", config.exclusions)
    graph = build_review_graph(checkpointer)
    run_config = {"configurable": {"thread_id": thread}}

    state = graph.get_state(run_config)
    excluded: list[str] = []
    if not state.values or state.next:  # not reviewed yet in this run, or stopped part way: (re)start
        def all_excluded(paths):
            excluded.extend(paths)
            return 0

        code = review_commit(owner, repo, case["base_sha"], head, description, config=config, token=token,
                             publish=lambda report: None, all_excluded=all_excluded,
                             pr_number=case.get("pr_number"), checkpointer=checkpointer, trace_path=trace_path)
        if excluded:  # every changed file is excluded: nothing was reviewed, so nothing was raised or read
            return _review(case, config, files=[], incomplete=[], excluded=excluded, findings=[], files_read=[],
                           trace=None)
        state = graph.get_state(run_config)
        if not state.values or state.next:
            raise ReviewFailed(f"the review of {case['repo']}@{head[:7]} left no result (exit code {code}); "
                               "see the review's log above")

    results = state.values["results"]
    findings = [{"file": i["file"], "line": i["line"], "severity": i["severity"], "summary": i["comment"],
                 "suggestion": i["suggestion"]} for f in state.values["files"] for i in results[f]["issues"]]
    return _review(case, config, files=state.values["files"],
                   incomplete=[f for f in state.values["files"] if results[f]["incomplete"]],
                   excluded=[], findings=findings, trace=str(trace_path),
                   **_files_read(state.values["files"], load_trace(trace_path)))


def _files_read(files: list[str], spans: dict) -> dict:
    """The paths read in the whole review, from its trace; nothing if a file has no span (reviewed before
    the trace existed): [] would say the agent read nothing, and the judge counts claims about unread
    code as false."""
    if any(f not in spans for f in files):
        return {}
    return {"files_read": sorted({p for f in files for p in spans[f]["span"]["output"]["files_read"]})}


def _review(case, config, **fields) -> dict:
    return {"repo": case["repo"], "pr_number": case.get("pr_number"), "base_sha": case["base_sha"],
            "head_sha": case["raised_sha"], "model": config.model, **fields}
