"""Golden master (characterisation) of `main()` on a fixture PR (PMAT-314).

GitHub is faked at the network boundary only: `subprocess.run` (the `gh` CLI) and httpx's
transport (every REST call). No name inside review_pull_request is patched apart from the
config and the chat model, so moving code between modules cannot silently bypass the fakes.

The snapshot holds what reaches the model, the SHA every file and tree was read at, the diff
range requested and the comment posted. A full review's `gh pr diff N` is recorded as the PR's
`base...head` range, the same as a compare request for it (the two diffs are byte-identical on
real PRs, PMAT-314 acceptance test (d)).

Regenerate only on purpose: GOLDEN_UPDATE=1 uv run pytest tests/test_golden_master.py
"""

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from review_pull_request.config import Config, SENSITIVE_PATTERNS
from review_pull_request.review import main
from tests.helpers import chat_model_factory

GOLDEN = Path(__file__).parent / "fixtures" / "golden_master.json"

OWNER, REPO, PR = "acme", "shop", 7
PR_URL = f"https://github.com/{OWNER}/{REPO}/pull/{PR}"
BASE, HEAD, LAST, NEW = "1" * 40, "2" * 40, "3" * 40, "4" * 40
TOKEN = "ghp_golden0000000000000000000000000000"
LOGIN = "review-bot"
DESCRIPTION = "Adds a cart total."
LAST_REVIEW = f"# AI Code Review (full review of {LAST[:7]})\n\n> **Reviewed commit:** `{LAST}`\n"


def _file_diff(path, body):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n+{body}\n"


DIFFS = {
    f"{BASE}...{HEAD}": _file_diff("src/cart.py", "total = sum(prices)")
    + _file_diff("src/util.py", "def add(a, b): return a + b")
    + _file_diff(".env", "TOKEN=golden-secret"),
    f"{LAST}...{HEAD}": _file_diff("src/cart.py", "total = sum(prices)"),
}
FILES = {"src/cart.py": "total = sum(prices)\n", "src/util.py": "def add(a, b): return a + b\n"}
PR_VIEW = {"headRefName": "feature/cart", "baseRefName": "master", "headRefOid": HEAD, "baseRefOid": BASE}


class FakeGitHub:
    """Answers `gh` and GitHub REST calls for one fixture PR; records what the review asked for."""

    def __init__(self):
        self.diff_ranges = []
        self.reads = []  # (path, ref)
        self.trees = []  # ref
        self.posted = []
        self.unexpected = []

    def run(self, cmd, *args, **kwargs):
        def done(stdout):
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        if cmd[:3] == ["gh", "auth", "token"]:
            return done(TOKEN + "\n")
        if cmd[:3] == ["gh", "pr", "view"] and cmd[3] == str(PR):
            fields = cmd[cmd.index("--json") + 1].split(",")
            return done(json.dumps({f: PR_VIEW[f] for f in fields}))
        if cmd[:3] == ["gh", "pr", "diff"] and cmd[3] == str(PR):
            self.diff_ranges.append(f"{BASE}...{HEAD}")
            return done(DIFFS[f"{BASE}...{HEAD}"])
        self.unexpected.append(" ".join(map(str, cmd)))
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected command")

    def handle(self, request: httpx.Request) -> httpx.Response:
        url, method = request.url, request.method
        path = url.path  # httpx has already decoded it
        repo = f"/repos/{OWNER}/{REPO}"
        if url.host == "api.github.com":
            if method == "GET" and path == f"{repo}/pulls/{PR}":
                return httpx.Response(200, json={"body": DESCRIPTION})
            if method == "GET" and path == f"{repo}/issues/{PR}/comments":
                return httpx.Response(200, json=[{"user": {"login": LOGIN}, "body": LAST_REVIEW,
                                                  "created_at": "2026-10-01T10:00:00Z"}])
            if method == "POST" and path == f"{repo}/issues/{PR}/comments":
                self.posted.append(json.loads(request.content)["body"])
                return httpx.Response(201, json={"html_url": f"{PR_URL}#issuecomment-1"})
            if method == "GET" and path == "/user":
                return httpx.Response(200, json={"login": LOGIN})
            if method == "GET" and path.startswith(f"{repo}/compare/"):
                rng = path.removeprefix(f"{repo}/compare/")
                if rng in DIFFS and "diff" in request.headers.get("Accept", ""):
                    self.diff_ranges.append(rng)
                    return httpx.Response(200, text=DIFFS[rng])
                if rng == f"{LAST}...{HEAD}":
                    return httpx.Response(200, json={"status": "ahead", "total_commits": 1,
                                                     "commits": [{"sha": NEW}], "files": [{}]})
            if method == "GET" and path.startswith(f"{repo}/git/trees/"):
                self.trees.append(path.removeprefix(f"{repo}/git/trees/"))
                return httpx.Response(200, json={"truncated": False, "tree": [
                    {"path": p, "type": "blob", "mode": "100644"} for p in [*FILES, ".env"]]})
            if method == "GET" and path.startswith(f"{repo}/contents/"):
                file_path, ref = path.removeprefix(f"{repo}/contents/"), url.params.get("ref")
                self.reads.append([file_path, ref])
                if file_path in FILES:
                    return httpx.Response(200, text=FILES[file_path])
                return httpx.Response(404, json={"message": "Not Found"})
        self.unexpected.append(f"{method} {url}")
        return httpx.Response(599, text="unexpected request")


class Model:
    """Overview: fixed text. Per file: reads the file under review once, then reports one issue."""

    def __init__(self):
        self.seen = []

    def __call__(self, messages, tools):
        self.seen.append(messages)
        if tools is None:
            return SimpleNamespace(content="Overview: adds a cart total.", tool_calls=[])
        user = next(m["content"] for m in messages if m["role"] == "user")
        target = next((p for p in FILES if p in user), None)
        if target and not any(m["role"] == "tool" for m in messages):
            call = SimpleNamespace(id=f"call-{target}", name="read_file",
                                   arguments={"path": target, "reason": "golden"})
            return SimpleNamespace(content=None, tool_calls=[call])
        return SimpleNamespace(content=json.dumps({"summary": f"Reviewed {target}.", "issues": [
            {"file": target, "line": 1, "severity": "medium", "comment": f"Check {target}.",
             "suggestion": None}]}), tool_calls=[])


@pytest.fixture
def _no_github_reader():
    """Overrides conftest's stub reader: this test runs the real GitHubFileReader on the fake."""
    yield


def _run(argv):
    github, model = FakeGitHub(), Model()
    cfg = Config(model="m", api_url="http://x", api_token="t", exclusions=list(SENSITIVE_PATTERNS))
    with patch("review_pull_request.review.load_config", return_value=cfg), \
            patch("review_pull_request.session.make_chat_model", chat_model_factory(model)), \
            patch("subprocess.run", github.run), \
            patch.object(httpx.HTTPTransport, "handle_request", lambda self, req: github.handle(req)):
        code = main(argv)
    return {"exit": code, "unexpected": github.unexpected, "diff_ranges": github.diff_ranges,
            "trees": github.trees, "reads": github.reads, "model": model.seen, "posted": github.posted}


def _snapshot():
    return {"full": _run([PR_URL]), "since_last_review": _run([PR_URL, "--since-last-review"])}


def test_main_matches_the_golden_master():
    actual = json.loads(json.dumps(_snapshot()))
    if os.environ.get("GOLDEN_UPDATE") == "1":
        GOLDEN.write_text(json.dumps(actual, indent=1) + "\n")
    expected = json.loads(GOLDEN.read_text())
    for mode in ("full", "since_last_review"):
        assert actual[mode]["model"], f"{mode}: the model was never called, so the snapshot proves nothing"
        assert actual[mode]["reads"], f"{mode}: no file was read through the GitHub fake"
        assert actual[mode]["posted"], f"{mode}: nothing was posted"
        for key in expected[mode]:
            assert actual[mode][key] == expected[mode][key], f"{mode}: {key} differs from the golden master"
