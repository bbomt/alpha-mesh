"""End-to-end canary: excluded/sensitive content must never reach the model.

A unique secret is planted in every input path (diff, case variants, nested dotenv files,
incremental diff, read_file requests). The real pipeline runs against a fake model that
records every message it receives; the secret must appear in none of them.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from review_pull_request.config import Config, SENSITIVE_PATTERNS
from review_pull_request.review import main
from tests.helpers import http_llm_factory

CANARY = "CANARY-7f3a9c-do-not-leak"
LAST_REVIEW = f"# AI Code Review (full review of aaaaaaa)\n\n> **Reviewed commit:** `{'a' * 40}`\n"
GITHUB_TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"  # what `gh auth token` would print

SECRET_PATHS = [".env", ".ENV", "svc/.env.production", ".claude/settings.json",
                ".Claude/settings.local.json", ".mesh/MEMORY.md", "config/active.json"]


READ_ATTEMPTS = (".env", "./.env", "/.env", ".ENV", "svc/../.env",
                 ".claude/settings.json", "config/active.json", "src/app.py")


def _file_diff(path, body):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n+{body}\n"


def _diff_with_secrets():
    return "".join(_file_diff(p, f"TOKEN={CANARY}") for p in SECRET_PATHS) + _file_diff("src/app.py", "x = 1")


class FakeReader:
    """Stands in for the GitHub file reader: serves the canary for every path except src/app.py."""

    def __init__(self, *a, **k):
        self.asked = []

    def read_file(self, path, max_lines):
        self.asked.append(path)
        return "x = 1\n" if path == "src/app.py" else f"TOKEN={CANARY}"

    def close(self):
        pass


class RecordingModel:
    """Fake chat(): records every message; asks to read every secret path, then answers."""

    def __init__(self):
        self.seen = []  # every message list the model was shown
        self.requested = []  # every path the model asked to read
        self._asked = {}

    def __call__(self, messages, tools):
        from types import SimpleNamespace

        self.seen.append(json.dumps(messages))
        if tools is None:  # overview pass
            return SimpleNamespace(content="Overview text.", tool_calls=[])
        key = messages[0]["content"]  # the per-file prompt: stable within a conversation (ids are not)
        paths = self._asked.setdefault(key, list(READ_ATTEMPTS))
        if paths:
            p = paths.pop(0)
            self.requested.append(p)
            call = SimpleNamespace(id=f"c-{p}", name="read_file", arguments={"path": p, "reason": "canary"})
            return SimpleNamespace(content=None, tool_calls=[call])
        return SimpleNamespace(content='{"summary": "ok", "issues": []}', tool_calls=[])


def _run(diff, extra_argv=(), incremental=False, exclusions=None):
    cfg = Config(model="m", api_url="http://x", api_token="t",
                 exclusions=list(SENSITIVE_PATTERNS) if exclusions is None else exclusions)  # as load_config builds it
    model = RecordingModel()
    patches = [
        patch("review_pull_request.review.load_config", return_value=cfg),
        patch("review_pull_request.review.parse_pr_url", return_value=("o", "r", 1)),
        patch("review_pull_request.review.get_pr_info", return_value={"headRefName": "b", "headRefOid": "abc123"}),
        patch("review_pull_request.review.get_pr_description", return_value="Adds an app."),
        patch("review_pull_request.review.GitHubFileReader", FakeReader),
        patch("review_pull_request.review.post_comment", return_value=True),
        patch("review_pull_request.session.HttpLLM", http_llm_factory(model)),
        patch("subprocess.run", return_value=MagicMock(returncode=0, stdout=GITHUB_TOKEN)),
    ]
    if incremental:
        patches += [
            patch("review_pull_request.review.find_last_review_comment",
                  return_value={"created_at": "2024-01-15T10:00:00Z", "body": LAST_REVIEW}),
            patch("review_pull_request.review.get_commits_between",
                  return_value=[{"sha": "c" * 40}]),
            patch("review_pull_request.review.build_incremental_diff", return_value=diff),
        ]
    else:
        patches.append(patch("review_pull_request.review.get_diff", return_value=diff))
    for p in patches:
        p.start()
    try:
        sys.argv = ["review", "https://github.com/o/r/pull/1", *extra_argv]
        main()
    finally:
        patch.stopall()
    return model


def test_canary_never_reaches_the_model_through_a_full_review():
    model = _run(_diff_with_secrets())

    assert model.seen, "the fake model was never called, so the test proves nothing"
    assert set(model.requested) == set(READ_ATTEMPTS), "every read attempt must actually be exercised"
    leaked = [m for m in model.seen if CANARY in m]
    assert not leaked, f"{len(leaked)} model call(s) contained the canary"


def test_canary_never_reaches_the_model_through_an_incremental_review():
    model = _run(_diff_with_secrets(), extra_argv=["--since-last-review"], incremental=True)

    assert model.seen
    assert not [m for m in model.seen if CANARY in m]


def test_the_ordinary_file_is_still_reviewed_and_readable():
    model = _run(_diff_with_secrets())

    per_file = model.seen[1:]  # seen[0] is the overview, which names every kept file anyway
    assert any("src/app.py" in m for m in per_file)
    assert "src/app.py" in model.requested


def test_the_harness_itself_detects_a_leak_when_protection_is_off():
    """Guards the guard: with no exclusions the canary must show up, or the tests above prove nothing."""
    model = _run(_diff_with_secrets(), exclusions=[])

    assert any(CANARY in m for m in model.seen)


def test_credentials_are_redacted_even_when_they_sit_in_an_allowed_file():
    """Defence in depth: a token pasted into a reviewable file is scrubbed at the model boundary."""
    leaky = _file_diff("src/app.py", f"KEY = '{GITHUB_TOKEN}'  # and sk-abcdefghijklmnopqrstuvwxyz")

    model = _run(leaky)

    assert model.seen
    assert not [m for m in model.seen if GITHUB_TOKEN in m or "sk-abcdefghij" in m]
    assert any("[REDACTED]" in m for m in model.seen)


# Diff header shapes that once let an excluded file's content through (PR #8 pass 15).
_RENAMED_OUT = (  # .env renamed to an allowed name with edits: its old lines stay in the hunk
    "diff --git a/.env b/notes.md\nsimilarity index 80%\nrename from .env\nrename to notes.md\n"
    f"--- a/.env\n+++ b/notes.md\n@@ -1,2 +1,2 @@\n-TOKEN={CANARY}\n KEEP={CANARY}\n+x\n"
)
_SPACED = _file_diff("secret dir/.env", f"TOKEN={CANARY}")
_QUOTED = (  # git quotes paths with special characters
    'diff --git "a/caf\\303\\251/.env" "b/caf\\303\\251/.env"\n'
    '--- "a/caf\\303\\251/.env"\n+++ "b/caf\\303\\251/.env"\n'
    f"@@ -1 +1 @@\n+TOKEN={CANARY}\n"
)


_FORGED_SECTION = (  # a form feed inside .env's hunk: splitlines() would start a new "file" there
    f"diff --git a/.env b/.env\n--- a/.env\n+++ b/.env\n@@ -1,2 +1,2 @@\n"
    f"+x\x0cdiff --git a/decoy.py b/decoy.py\n+TOKEN={CANARY}\n"
)


@pytest.mark.parametrize("shape", [_RENAMED_OUT, _SPACED, _QUOTED, _FORGED_SECTION],
                         ids=["renamed-out", "spaced", "quoted", "forged-section"])
def test_canary_never_reaches_the_model_through_unusual_diff_headers(shape):
    model = _run(shape + _file_diff("src/app.py", "x = 1"))

    assert model.seen
    assert not [m for m in model.seen if CANARY in m]


@pytest.mark.parametrize("shape", [_RENAMED_OUT, _SPACED, _QUOTED, _FORGED_SECTION],
                         ids=["renamed-out", "spaced", "quoted", "forged-section"])
def test_the_harness_detects_unusual_header_leaks_when_protection_is_off(shape):
    model = _run(shape + _file_diff("src/app.py", "x = 1"), exclusions=[])

    assert CANARY in model.seen[0], "the overview prompt (the diff) should carry the canary"
