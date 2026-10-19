"""Runs the real CLI entry (review.main) in its own process, with fakes at the seams (spec 5.1).

Durable checkpoints go to a SqliteSaver file, which survives `kill -9`. The model and GitHub are
fakes driven by RR_* environment variables, and record what they were asked in RR_DIR:

    RR_DIR      work dir: diff.txt (full diff), inc_diff.txt (incremental diff),
                calls.jsonl (one line per model call), posts.jsonl (one line per posted comment)
    RR_DB       the SqliteSaver file
    RR_HEAD     PR head SHA
    RR_EXCLUDE  comma-separated exclusion patterns
    RR_TOKEN    the model API token (redacted before the model sees it)
    RR_HANG     "overview" or a file path: that call blocks forever
    RR_FAIL     a file path: that file's review raises LLMError
    RR_ECHO     "1": each file summary repeats the prompt the model was given (canary tests)
    RR_GH_TOKEN the GitHub token (redacted before the model sees it)
    RR_STORE    "aws": the real DynamoDB/S3 checkpointer instead of SqliteSaver (integration test I1)
    RR_LEAK     "1": a test-only graph variant that also saves the raw diff in the state, to show
                the checkpoint scan in test_canary.py can find a leak

Usage: python tests/fixtures/run_review.py <review-pull-request args...>
"""

import contextlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402

from review_pull_request import review  # noqa: E402
from review_pull_request import graph  # noqa: E402
from review_pull_request.config import SENSITIVE_PATTERNS, Config  # noqa: E402
from review_pull_request.constants import REVIEW_SIGNATURE  # noqa: E402
from review_pull_request.llm import LLMError  # noqa: E402
from tests.helpers import FakeChatModel, FakeLLM, StubReader  # noqa: E402

WORK = Path(os.environ["RR_DIR"])
CALLS = WORK / "calls.jsonl"
POSTS = WORK / "posts.jsonl"


def _append(path: Path, record: dict) -> None:
    with path.open("a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()
        os.fsync(f.fileno())


def _posts() -> list[str]:
    return [json.loads(line)["body"] for line in POSTS.read_text().splitlines()] if POSTS.exists() else []


def responder_for(model: str):
    def respond(messages, tools):
        prompt = messages[-1]["content"] if messages else ""
        if tools is None:
            _append(CALLS, {"kind": "overview", "model": model})
            if os.environ.get("RR_HANG") == "overview":
                time.sleep(3600)
            return SimpleNamespace(content=f"Overview by {model}.", tool_calls=[])
        path = re.search(r"^\+\+\+ b/(\S+)", prompt, re.M).group(1)
        _append(CALLS, {"kind": "file", "file": path, "model": model})
        if os.environ.get("RR_HANG") == path:
            time.sleep(3600)
        if os.environ.get("RR_FAIL") == path:
            raise LLMError("API returned 503")
        summary = prompt if os.environ.get("RR_ECHO") == "1" else f"summary of {path}"
        issue = {"file": path, "line": 1, "severity": "medium", "comment": f"finding-in-{path}",
                 "suggestion": ""}
        return SimpleNamespace(content=json.dumps({"summary": summary, "issues": [issue]}), tool_calls=[])
    return respond


def load_config(args):
    exclude = [p for p in os.environ.get("RR_EXCLUDE", "").split(",") if p]
    return Config(model=args.model or "fake/model-a", api_url="https://openrouter.ai/api",
                  api_token=os.environ.get("RR_TOKEN", "model-token"), exclusions=exclude + SENSITIVE_PATTERNS)


def leaking_graph():
    """The review graph, but its state also holds the raw diff (excluded files included)."""
    class LeakyState(graph.ReviewState):
        leak: str

    real_overview = graph.overview

    def overview(state, runtime):
        return {**real_overview(state, runtime), "leak": (WORK / "diff.txt").read_text()}

    return patch.multiple(graph, ReviewState=LeakyState, overview=overview)


def find_last_review_comment(owner, repo, pr_number, token):
    reviews = [b for b in _posts() if b.startswith(REVIEW_SIGNATURE)]
    return {"body": reviews[-1]} if reviews else None


def post_comment(body, pr_url, config):
    _append(POSTS, {"body": body})
    return True


def main() -> int:
    head = os.environ.get("RR_HEAD", "head111")
    saver = (None if os.environ.get("RR_STORE") == "aws"
             else SqliteSaver(sqlite3.connect(os.environ["RR_DB"], check_same_thread=False)))
    leak = leaking_graph() if os.environ.get("RR_LEAK") == "1" else contextlib.nullcontext()
    with leak, patch.object(review, "load_config", load_config), \
         patch.object(review, "parse_pr_url", lambda url: ("owner", "repo", 42)), \
         patch.object(review, "get_pr_info", lambda *a: {"headRefName": "b", "headRefOid": head}), \
         patch.object(review, "get_pr_description", lambda *a: ""), \
         patch.object(review, "_get_github_token", lambda: os.environ.get("RR_GH_TOKEN", "gh-token")), \
         patch.object(review, "get_diff", lambda *a: (WORK / "diff.txt").read_text()), \
         patch.object(review, "find_last_review_comment", find_last_review_comment), \
         patch.object(review, "get_commits_between", lambda *a: [{"sha": head}]), \
         patch.object(review, "build_incremental_diff", lambda *a: (WORK / "inc_diff.txt").read_text()), \
         patch.object(review, "post_comment", post_comment), \
         patch.object(review, "GitHubFileReader", lambda *a, **k: StubReader()), \
         patch("review_pull_request.session.make_chat_model",
               lambda config, model: FakeChatModel(llm=FakeLLM(responder_for(model)))):
        return review.main(sys.argv[1:], checkpointer=saver)


if __name__ == "__main__":
    sys.exit(main())
