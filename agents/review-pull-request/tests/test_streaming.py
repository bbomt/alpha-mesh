"""PMAT-278: the review graph runs with stream_events (v3) instead of invoke.

Same start/resume/skip, checkpoints, report and post as invoke; node events come out in order.
"""

import sys
from unittest.mock import MagicMock, patch

from review_pull_request.config import Config
from review_pull_request.review import main
from tests.helpers import chat_model_factory

DIFF = "".join(f"diff --git a/src/{n}.py b/src/{n}.py\n--- a/src/{n}.py\n+++ b/src/{n}.py\n@@ -1 +1 @@\n+{n} = 1\n"
               for n in ("a", "b"))


def _responder(messages, tools):
    if tools is None:
        return MagicMock(content="An overview.", tool_calls=[])
    return MagicMock(content='{"summary": "fine", "issues": [{"line": 1, "severity": "low", '
                             '"comment": "nit", "suggestion": "s"}]}', tool_calls=[])


def _run(argv=(), runner=None):
    """main() on a two-file PR with fake model and GitHub; returns (rc, posted body, stderr lines)."""
    config = Config(model="m", api_url="http://x", api_token="secret-token-123",
                    verbose="--verbose" in argv)  # load_config is patched, so set it as it would
    patches = [
        patch("review_pull_request.review.load_config", return_value=config),
        patch("review_pull_request.review.parse_pr_url", return_value=("owner", "repo", 42)),
        patch("review_pull_request.review.get_pr_info", return_value={"headRefName": "b", "headRefOid": "abc"}),
        patch("review_pull_request.review.get_pr_description", return_value=""),
        patch("review_pull_request.review.get_diff", return_value=DIFF),
        patch("review_pull_request.session.make_chat_model", chat_model_factory(_responder)),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="ghtoken")),
    ]
    if runner:
        patches.append(patch("review_pull_request.review.run_review", runner))
    with patch("review_pull_request.review.post_comment", return_value=True) as post, \
         patch.object(sys, "argv", list(sys.argv)):
        for p in patches:
            p.start()
        try:
            sys.argv = ["review", "https://github.com/owner/repo/pull/42", *argv]
            rc = main()
        finally:
            for p in patches:
                p.stop()
    return rc, post.call_args[0][0] if post.called else None


def _invoke(graph, graph_input, run_config, context, on_node):
    """The pre-PMAT-278 way of running the graph, for comparison."""
    return graph.invoke(graph_input, run_config, context=context, durability="sync")


def test_verbose_run_reports_node_events_in_order(capsys):
    rc, body = _run(["--verbose"])

    nodes = [line.split("node: ", 1)[1] for line in capsys.readouterr().err.splitlines() if "node: " in line]
    assert rc == 0 and body
    assert nodes == ["overview", "review_next_file", "review_next_file", "report", "post"]


def test_node_events_are_not_printed_without_verbose(capsys):
    _run()

    assert "node: " not in capsys.readouterr().err


def test_the_streamed_run_posts_the_same_report_as_invoke():
    rc_stream, streamed = _run()
    rc_invoke, invoked = _run(runner=_invoke)

    assert rc_stream == rc_invoke == 0
    assert streamed == invoked
    assert "src/a.py" in streamed and "src/b.py" in streamed and "nit" in streamed


def test_node_events_arrive_while_the_graph_runs_not_after_it():
    """Each node's event comes before that node's model call, so progress is live."""
    from langgraph.checkpoint.memory import InMemorySaver

    from review_pull_request.graph import ReviewContext, build_review_graph, initial_state, run_review
    from review_pull_request.safe_diff import SafeDiff
    from review_pull_request.session import ReviewSession
    from tests.helpers import StubReader

    order = []

    def responder(messages, tools):
        order.append("model")
        return _responder(messages, tools)

    config = Config(model="m", api_url="http://x", api_token="secret-token-123")
    with patch("review_pull_request.session.make_chat_model", chat_model_factory(responder)):
        session = ReviewSession.create(config, inner_reader=StubReader())
    diff = SafeDiff.from_raw(DIFF, session.policy)
    context = ReviewContext(session=session, diff=diff, pr_description="", model="m",
                            render=lambda state: "report", publish=lambda report: order.append("published"))

    run_review(build_review_graph(InMemorySaver()), initial_state(diff), {"configurable": {"thread_id": "t"}},
               context, on_node=lambda name: order.append(name))

    assert order == ["overview", "model", "review_next_file", "model", "review_next_file", "model",
                     "report", "post", "published"]
