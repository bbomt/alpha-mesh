"""The eval adapter (PMAT-304): a case -> the review core's inputs -> the review's findings.

`mesh review-pull-request` (the PR adapter) and this adapter call the same core, review_commit,
with (owner, repo, base, head, description): here the case's base_sha, its raised_sha and the PR's
description. Nothing is posted. The findings come from the review's final graph state (the data the
report is rendered from), read back from the checkpointer this adapter passes in.
"""

from review_pull_request.checkpoints import thread_id
from review_pull_request.graph import build_review_graph
from review_pull_request.review import review_commit


class ReviewFailed(Exception):
    """The review left no finished result (model unusable, GitHub error...)."""


def review_case(case: dict, *, config, token: str, description: str, checkpointer) -> dict:
    """Review the case's commit, or reuse the finished review of it in this checkpointer."""
    owner, repo = case["repo"].split("/")
    head = case["raised_sha"]
    thread = thread_id(owner, repo, case["pr_number"], head, "full", config.exclusions)
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
                             pr_number=case["pr_number"], checkpointer=checkpointer)
        if excluded:  # every changed file is excluded: nothing was reviewed, so nothing was raised
            return _review(case, config, files=[], incomplete=[], excluded=excluded, findings=[])
        state = graph.get_state(run_config)
        if not state.values or state.next:
            raise ReviewFailed(f"the review of {case['repo']}@{head[:7]} left no result (exit code {code}); "
                               "see the review's log above")

    results = state.values["results"]
    findings = [{"file": i["file"], "line": i["line"], "severity": i["severity"], "summary": i["comment"],
                 "suggestion": i["suggestion"]} for f in state.values["files"] for i in results[f]["issues"]]
    return _review(case, config, files=state.values["files"],
                   incomplete=[f for f in state.values["files"] if results[f]["incomplete"]],
                   excluded=[], findings=findings)


def _review(case, config, **fields) -> dict:
    return {"repo": case["repo"], "pr_number": case["pr_number"], "base_sha": case["base_sha"],
            "head_sha": case["raised_sha"], "model": config.model, **fields}
