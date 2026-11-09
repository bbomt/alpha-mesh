"""Acceptance tests for the review trace (PMAT-325): every tool call and its result, exactly as the
model received them, in a local JSONL file shaped like Langfuse observations.

The review core runs with a scripted model and a stub reader; the model records every message list
it is sent, so the trace can be compared with what the model actually got.
"""

import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

from langgraph.checkpoint.sqlite import SqliteSaver

from review_pull_request import trace
from review_pull_request.config import Config, SENSITIVE_PATTERNS
from review_pull_request.llm import LLMError
from review_pull_request.review import main, review_commit
from tests.helpers import StubReader, chat_model_factory

FILES = {"src/a.py": "a = 1\n", "src/b.py": "b = 2\n", "src/helper.py": "def helper(): ...\n",
         ".env": "TOKEN=x\n"}

# Per file under review: the read_file calls the model makes, one per turn, then its answer.
READS = {"src/a.py": ["src/helper.py", "src/missing.py", ".env", "src/helper.py"],
         "src/b.py": ["src/helper.py"]}


def _diff(paths):
    return "".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n+x\n" for p in paths)


class ScriptedModel:
    def __init__(self, fail_on=None):
        self.sent = []  # (file under review, messages) for every per-file call
        self.fail_on = fail_on

    def __call__(self, messages, tools):
        if "## Tool use" not in messages[0]["content"]:  # the overview pass, not a per-file review
            return SimpleNamespace(content="Overview.", tool_calls=[])
        target = next(p for p in READS if f"+++ b/{p}" in messages[0]["content"])
        self.sent.append((target, messages))
        if target == self.fail_on:
            raise LLMError("API returned 503")
        done = sum(1 for m in messages if m["role"] == "tool")
        if done < len(READS[target]):
            path = READS[target][done]
            call = SimpleNamespace(id=f"{target}-{done}", name="read_file",
                                   arguments={"path": path, "reason": "check"})
            return SimpleNamespace(content=None, tool_calls=[call])
        return SimpleNamespace(content='{"summary": "ok", "issues": []}', tool_calls=[])


def _config():
    return Config(model="m", api_url="http://x", api_token="t", exclusions=list(SENSITIVE_PATTERNS))


def _review(trace_path, model, checkpointer=None, files=("src/a.py", "src/b.py")):
    with patch("review_pull_request.review.GitHubFileReader", lambda *a, **k: StubReader(dict(FILES))), \
            patch("review_pull_request.review.get_diff", return_value=_diff(files)), \
            patch("review_pull_request.session.make_chat_model", chat_model_factory(model)):
        return review_commit("o", "r", "b" * 40, "c" * 40, "Adds a and b.", config=_config(), token="tok",
                             publish=lambda report: None, all_excluded=lambda paths: 0,
                             checkpointer=checkpointer, trace_path=trace_path)


def _records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def _tools(path, file=None):
    spans = {r["id"]: r["input"]["file"] for r in _records(path) if r["type"] == "span"}
    return [r for r in _records(path) if r["type"] == "tool"
            and (file is None or spans[r["parent_observation_id"]] == file)]


# 1. Exact results ────────────────────────────────────────────────────────────────────────────────

def test_the_trace_holds_each_tool_result_exactly_as_the_model_received_it_in_order(tmp_path):
    path, model = tmp_path / "trace.jsonl", ScriptedModel()

    assert _review(path, model) == 0

    for file in READS:
        last = [m for f, m in model.sent if f == file][-1]  # the final call was sent every result
        sent = [m for m in last if m["role"] == "tool"]
        calls = [tc for m in last if m["role"] == "assistant" for tc in m.get("tool_calls") or []]
        traced = _tools(path, file)
        assert [t["output"] for t in traced] == [m["content"] for m in sent]
        assert [t["name"] for t in traced] == [c["function"]["name"] for c in calls]
        assert [t["input"] for t in traced] == [json.loads(c["function"]["arguments"]) for c in calls]
    outputs = [t["output"] for t in _tools(path, "src/a.py")]
    assert "def helper" in outputs[0]
    assert "not found" in outputs[1] and "Nearest existing folder" in outputs[1]  # the hints the model got
    assert "excluded" in outputs[2]
    assert "already read" in outputs[3]


def test_the_trace_is_shaped_like_langfuse_observations(tmp_path):
    path = tmp_path / "trace.jsonl"

    _review(path, ScriptedModel())

    records = _records(path)
    head = records[0]
    assert head["type"] == "trace" and head["metadata"]["repo"] == "o/r" and head["metadata"]["head_sha"] == "c" * 40
    spans = [r for r in records if r["type"] == "span"]
    assert [s["input"]["file"] for s in spans] == ["src/a.py", "src/b.py"]
    assert all(s["trace_id"] == head["id"] and s["start_time"] <= s["end_time"] for s in spans)
    assert spans[0]["output"]["files_read"] == ["src/helper.py"]
    assert all(t["trace_id"] == head["id"] and t["start_time"] for t in _tools(path))


def test_load_keeps_the_last_span_of_each_file(tmp_path):
    """A span written but its checkpoint not saved (killed in between): the file is reviewed again."""
    path = tmp_path / "trace.jsonl"
    _review(path, ScriptedModel())
    lines = path.read_text().splitlines()
    path.write_text("\n".join(lines + lines[1:]) + "\n")  # every span and tool call twice

    spans = trace.load(path)

    assert list(spans) == ["src/a.py", "src/b.py"]
    assert [len(s["tools"]) for s in spans.values()] == [4, 1]


# 4. Resume ───────────────────────────────────────────────────────────────────────────────────────

def test_a_review_stopped_part_way_and_resumed_has_each_tool_call_once(tmp_path):
    path = tmp_path / "trace.jsonl"
    saver = SqliteSaver(sqlite3.connect(tmp_path / "cp.sqlite", check_same_thread=False))

    assert _review(path, ScriptedModel(fail_on="src/b.py"), saver) == 1
    assert [s["input"]["file"] for s in _records(path) if s["type"] == "span"] == ["src/a.py"]
    resumed = ScriptedModel()
    assert _review(path, resumed, saver) == 0

    assert {f for f, _ in resumed.sent} == {"src/b.py"}, "the saved file must not be reviewed again"
    records = _records(path)
    assert [r["type"] for r in records].count("trace") == 1
    assert [s["input"]["file"] for s in records if s["type"] == "span"] == ["src/a.py", "src/b.py"]
    assert [t["input"]["path"] for t in _tools(path)] == [*READS["src/a.py"], *READS["src/b.py"]]


def test_a_resume_after_a_kill_mid_write_ignores_the_torn_line(tmp_path):
    """Killed during _write: the last line is cut short. The resume must still open and append."""
    path = tmp_path / "trace.jsonl"
    saver = SqliteSaver(sqlite3.connect(tmp_path / "cp.sqlite", check_same_thread=False))
    assert _review(path, ScriptedModel(fail_on="src/b.py"), saver) == 1
    with path.open("a") as f:
        f.write('{"type": "span", "id": "torn')  # no newline: the process died here

    assert _review(path, ScriptedModel(), saver) == 0

    assert list(trace.load(path)) == ["src/a.py", "src/b.py"]


def test_a_fresh_review_replaces_an_old_trace(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('{"type": "trace", "id": "an-earlier-review"}\n')

    _review(path, ScriptedModel())

    assert "an-earlier-review" not in path.read_text()


# 5. PR mode ──────────────────────────────────────────────────────────────────────────────────────

def test_mesh_review_pull_request_writes_a_trace_under_mesh_review_traces(tmp_path):
    model = ScriptedModel()
    cfg = _config()
    with patch("review_pull_request.review.load_config", return_value=cfg), \
            patch("review_pull_request.review.parse_pr_url", return_value=("o", "r", 1)), \
            patch("review_pull_request.review.get_pr_info", return_value={"headRefName": "b", "headRefOid": "c" * 40}), \
            patch("review_pull_request.review.get_pr_description", return_value="Adds a and b."), \
            patch("review_pull_request.review.GitHubFileReader", lambda *a, **k: StubReader(dict(FILES))), \
            patch("review_pull_request.review.get_diff", return_value=_diff(READS)), \
            patch("review_pull_request.review.post_comment", return_value=True), \
            patch("review_pull_request.session.make_chat_model", chat_model_factory(model)), \
            patch("review_pull_request.review._get_github_token", return_value="tok"):
        main(["https://github.com/o/r/pull/1", "--no-post-comment"])

    traces = list((tmp_path / ".mesh" / "review-traces").glob("*.jsonl"))  # conftest sets HOME to tmp_path
    assert len(traces) == 1
    assert len(_tools(traces[0])) == 5
