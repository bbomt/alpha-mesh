"""The review as a LangGraph graph (spec PMAT-237, 3.1-3.3).

    START -> overview -> review_next_file (once per file) -> report -> post -> END

Nodes call the unchanged stages. The state holds results only: file paths, model output and
small values; no diff, no file contents, no prompts. Everything else a node needs (the session,
the diff, how to render and publish) comes from the run's ReviewContext, which is never saved.
"""

import sys
import warnings
from dataclasses import asdict, dataclass
from typing import Any, Callable, NotRequired, TypedDict

from langchain_core._api.beta_decorator import LangChainBetaWarning
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.stream.transformers import TasksTransformer

from review_pull_request.safe_diff import SafeDiff
from review_pull_request.stage1 import run_overview
from review_pull_request.stage2 import run_file_review


class FileResult(TypedDict):
    file_path: str
    summary: str
    issues: list[dict]          # Issue as dict: file, line, severity, comment, suggestion
    tool_calls_made: int
    incomplete: bool
    missing: list[str]          # unrecovered not-found paths from this file's review
    model: str                  # model that produced this result
    files_read: NotRequired[list[str]]  # no longer written (PMAT-325: the trace holds it); read only from
                                        # results saved by PMAT-322's code. Remove once those checkpoints expire.


class ReviewState(TypedDict):
    files: list[str]                                   # files to review, in diff order
    overview: str | None
    overview_model: str | None
    results: dict[str, FileResult]                     # file_path -> result (plain field, no reducer)
    report: str | None


@dataclass
class ReviewContext:
    session: Any                          # ReviewSession: the only way to the model and files
    diff: SafeDiff
    pr_description: str
    model: str
    render: Callable[[ReviewState], str]  # state -> report text
    publish: Callable[[str], None]        # print and/or post the report; raises if posting fails


def initial_state(diff: SafeDiff) -> ReviewState:
    return ReviewState(files=[c.file_path for c in diff.chunks], overview=None, overview_model=None,
                       results={}, report=None)


def next_file(state: ReviewState) -> str | None:
    """The first file in diff order that has no result yet."""
    return next((f for f in state["files"] if f not in state["results"]), None)


def overview(state: ReviewState, runtime: Runtime[ReviewContext]) -> dict:
    ctx = runtime.context
    print("Stage 1: Overview pass...", file=sys.stderr)
    text = run_overview(ctx.diff, ctx.pr_description, ctx.session)
    print(f"Overview: {text[:80]}...", file=sys.stderr)
    return {"overview": text, "overview_model": ctx.model}


def review_next_file(state: ReviewState, runtime: Runtime[ReviewContext]) -> dict:
    ctx = runtime.context
    path = next_file(state)
    files = state["files"]
    print(f"[{files.index(path) + 1}/{len(files)}] reviewing {path}...", file=sys.stderr)
    chunk = next(c for c in ctx.diff.chunks if c.file_path == path)
    result = run_file_review(chunk, state["overview"], ctx.session)
    print(f"  → {len(result.issues)} issue(s), {result.tool_calls_made} tool call(s)", file=sys.stderr)
    missing = [p for p, asked_by in ctx.session.missing_paths.items() if path in asked_by]
    file_result = FileResult(file_path=path, summary=result.summary,
                             issues=[asdict(i) for i in result.issues],
                             tool_calls_made=result.tool_calls_made, incomplete=result.incomplete,
                             missing=missing, model=ctx.model)  # files_read is in the trace (PMAT-325)
    return {"results": {**state["results"], path: file_result}}


def after_review(state: ReviewState) -> str:
    return "review_next_file" if next_file(state) else "report"


def report(state: ReviewState, runtime: Runtime[ReviewContext]) -> dict:
    return {"report": runtime.context.render(state)}


def post(state: ReviewState, runtime: Runtime[ReviewContext]) -> dict:
    runtime.context.publish(state["report"])
    return {}


def build_review_graph(checkpointer):
    builder = StateGraph(ReviewState, context_schema=ReviewContext)
    builder.add_node("overview", overview)
    builder.add_node("review_next_file", review_next_file)
    builder.add_node("report", report)
    builder.add_node("post", post)
    builder.add_edge(START, "overview")
    builder.add_conditional_edges("overview", after_review, ["review_next_file", "report"])
    # One super-step per file. LangGraph >= 1.2's default recursion_limit is 10007 (it was 25 before),
    # far above any PR size (pyproject.toml requires langgraph>=1.2.11);
    # test_a_pr_with_many_files_is_reviewed_to_the_end runs 30 files through main().
    builder.add_conditional_edges("review_next_file", after_review, ["review_next_file", "report"])
    builder.add_edge("report", "post")
    builder.add_edge("post", END)
    return builder.compile(checkpointer=checkpointer)


# Node start/result events (run.tasks); v3 registers values, messages, lifecycle and subgraphs itself.
STREAM_TRANSFORMERS = (TasksTransformer,)


def run_review(graph, graph_input, run_config, context: ReviewContext,
               on_node: Callable[[str], None]) -> dict | None:
    """Run the graph like graph.invoke(..., durability="sync"), streaming its events (PMAT-278).

    Same start (input) / resume (None) and checkpoints; a node's exception propagates as with
    invoke. on_node(name) is called as each node starts. Only node names leave this function:
    event payloads hold state and model output (including what the model was shown via read_file),
    so they are never printed or logged.
    """
    with warnings.catch_warnings():  # v3 is chosen knowingly (PMAT-278); its beta notice is noise
        warnings.simplefilter("ignore", LangChainBetaWarning)
        run = graph.stream_events(graph_input, run_config, version="v3", context=context,
                                  durability="sync", transformers=list(STREAM_TRANSFORMERS))
    for task in run.tasks:
        if "result" not in task and "error" not in task:  # a start event
            on_node(task["name"])
    return run.output
