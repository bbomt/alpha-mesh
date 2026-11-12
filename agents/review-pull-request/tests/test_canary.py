"""End-to-end canary: excluded/sensitive content must never reach the model.

A unique secret is planted in every input path (diff, case variants, nested dotenv files,
incremental diff, read_file requests). The real pipeline runs against a fake model that
records every message it receives; the secret must appear in none of them.
"""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from review_pull_request.config import Config, SENSITIVE_PATTERNS
from review_pull_request.review import main, run_review_agent
from review_pull_request.guard import Redactor
from tests.helpers import chat_model_factory

CANARY = "CANARY-7f3a9c-do-not-leak"
LAST_REVIEW = f"# AI Code Review (full review of aaaaaaa)\n\n> **Reviewed commit:** `{'a' * 40}`\n"
GITHUB_TOKEN = "ghp_0123456789abcdefghijklmnopqrstuvwxyz"  # what `gh auth token` would print

SECRET_PATHS = [".env", ".ENV", "svc/.env.production", ".claude/settings.json",
                ".Claude/settings.local.json", ".mesh/MEMORY.md", "config/active.json"]


READ_ATTEMPTS = (".env", "./.env", "/.env", ".ENV", "svc/../.env",
                 ".claude/settings.json", "config/active.json", "src/app.py",
                 # not found: the "did you mean" hint must never name an excluded file
                 "missing/settings.json", "missing/active.json", "missing/app.py")


def _file_diff(path, body):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n+{body}\n"


def _diff_with_secrets():
    return "".join(_file_diff(p, f"TOKEN={CANARY}") for p in SECRET_PATHS) + _file_diff("src/app.py", "x = 1")


# The repo tree the fake reader lists: every secret path, plus excluded files beside src/app.py.
TREE = [*SECRET_PATHS, "src/app.py", "src/util.py", "src/.env", "src/.claude/settings.json"]
EXCLUDED_NAMES = (".env", ".ENV", ".claude", ".Claude", ".mesh", "active.json", "svc/", "config/")


class FakeReader:
    """Stands in for the GitHub file reader: serves the canary for every path except src/app.py."""

    def __init__(self, *a, **k):
        self.asked = []

    def read_file(self, path, max_lines):
        from review_pull_request.readers import NotFound

        self.asked.append(path)
        if path.startswith("missing/"):
            return NotFound(f"Error: file not found: {path}")
        return "x = 1\n" if path == "src/app.py" else f"TOKEN={CANARY}"

    def list_files(self):
        return list(TREE)

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
        patch("review_pull_request.session.make_chat_model", chat_model_factory(model)),
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


def _run_agent(diff, exclusions=None):
    """run_review_agent called directly, as the evals call it (no PR adapter in front)."""
    cfg = Config(model="m", api_url="http://x", api_token="t",
                 exclusions=list(SENSITIVE_PATTERNS) if exclusions is None else exclusions)
    model = RecordingModel()
    with patch("review_pull_request.review.GitHubFileReader", FakeReader), \
            patch("review_pull_request.review.get_diff", return_value=diff), \
            patch("review_pull_request.session.make_chat_model", chat_model_factory(model)):
        run_review_agent("o", "r", "b" * 40, "abc123", "Adds an app.", config=cfg, token=GITHUB_TOKEN,
                      publish=lambda report: None, all_excluded=lambda paths: 0)
    return model


def test_canary_never_reaches_the_model_through_run_review_agent():
    model = _run_agent(_diff_with_secrets())

    assert model.seen, "the fake model was never called, so the test proves nothing"
    assert set(model.requested) == set(READ_ATTEMPTS), "every read attempt must actually be exercised"
    assert not [m for m in model.seen if CANARY in m]


def test_the_run_agent_harness_detects_a_leak_when_protection_is_off():
    model = _run_agent(_diff_with_secrets(), exclusions=[])

    assert any(CANARY in m for m in model.seen)


def _tool_results(model):
    return [m["content"] for seen in model.seen for m in json.loads(seen) if m.get("role") == "tool"]


def _not_found_hints(model):
    """Everything after the "file not found: <requested path>" line: the hints and folder listings."""
    return [t.split("\n", 1)[1] for t in _tool_results(model) if "not found" in t and "\n" in t]


def _folder_listings(model):
    """The upfront folder listing line of every per-file prompt."""
    prompts = [json.loads(seen)[0]["content"] for seen in model.seen[1:]]  # seen[0] is the overview
    return [line for p in prompts for line in p.splitlines() if line.startswith("Files in the folder")]


def _names_an_excluded_file(text):
    return any(name in text for name in EXCLUDED_NAMES)


def test_not_found_hints_never_name_an_excluded_file():
    model = _run(_diff_with_secrets())

    hints = _not_found_hints(model)
    assert any("src/app.py" in h for h in hints), "the allowed file should be suggested, or this proves nothing"
    assert any("Nearest existing folder" in h for h in hints), "a folder listing should be offered, or this proves nothing"
    assert not [h for h in hints if _names_an_excluded_file(h)]


def test_the_harness_detects_an_excluded_hint_when_protection_is_off():
    model = _run(_diff_with_secrets(), exclusions=[])

    assert any(_names_an_excluded_file(h) for h in _not_found_hints(model))


def test_the_upfront_folder_listing_never_names_an_excluded_file():
    model = _run(_diff_with_secrets())

    listings = _folder_listings(model)
    assert listings and all("app.py" in line and "util.py" in line for line in listings)
    assert not [line for line in listings if _names_an_excluded_file(line)]


def test_the_harness_detects_an_excluded_folder_listing_when_protection_is_off():
    model = _run(_diff_with_secrets(), exclusions=[])

    assert any(_names_an_excluded_file(line) for line in _folder_listings(model))


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


# --- The checkpoint store (spec PMAT-237 3.2, 5.2: C2, C3, C5) -------------------------------------
# Invariant: excluded content and tokens never reach the checkpoint store, by any path. The store is
# the SqliteSaver file of tests/fixtures/run_review.py; the fake model echoes every per-file prompt
# into its summary (RR_ECHO), so anything that reached the model would reach the store too.

STORE_MODEL_TOKEN = "sk-or-v1-0123456789abcdefSTORECANARY"
STORE_GH_TOKEN = "ghp_STORECANARY0123456789abcdefghijklmnop"
STORE_SECRETS = (CANARY, STORE_MODEL_TOKEN, STORE_GH_TOKEN)


def _store_diff():
    return (_file_diff(".env", f"TOKEN={CANARY}")
            + _file_diff("src/app.py", f"KEY = '{STORE_MODEL_TOKEN}'")
            + _file_diff("src/b.py", f"GH = '{STORE_GH_TOKEN}'"))


def _store_text(db) -> str:
    """Every checkpoint, its metadata and every pending write in the store, as text."""
    import sqlite3

    con = sqlite3.connect(db)
    try:
        parts = []
        for (table,) in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'"):
            for row in con.execute(f'SELECT * FROM "{table}"'):
                parts.extend(v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v) for v in row)
        return "\n".join(parts)
    finally:
        con.close()


def _store_review(tmp_path, **env):
    from tests.test_resume import Review

    r = Review(tmp_path)
    (tmp_path / "diff.txt").write_text(_store_diff())
    r.env.update(RR_ECHO="1", RR_TOKEN=STORE_MODEL_TOKEN, RR_GH_TOKEN=STORE_GH_TOKEN, **env)
    return r, tmp_path / "checkpoints.sqlite"


def test_c2_no_secret_reaches_the_checkpoint_store_in_a_full_killed_and_resumed_run(tmp_path):
    r, db = _store_review(tmp_path)

    r.kill_while_hung("src/b.py")  # src/app.py's result is saved, src/b.py is in flight
    killed = _store_text(db)
    assert r.run().returncode == 0  # resume
    resumed = _store_text(db)
    assert r.run("--force-rerun").returncode == 0  # a full run from the start
    full = _store_text(db)

    for stage, text in (("killed", killed), ("resumed", resumed), ("full", full)):
        assert "src/app.py" in text and "[REDACTED]" in text, f"{stage}: the scan saw no results"
        for secret in STORE_SECRETS:
            assert secret not in text, f"{stage}: {secret} is in the checkpoint store"


def test_c3_the_store_scan_finds_a_deliberate_leak(tmp_path):
    r, db = _store_review(tmp_path, RR_LEAK="1")

    assert r.run().returncode == 0

    text = _store_text(db)
    assert all(secret in text for secret in STORE_SECRETS)


def test_c5_the_state_holds_only_the_listed_fields():
    from review_pull_request.graph import FileResult, ReviewState

    assert set(ReviewState.__annotations__) == {"files", "overview", "overview_model", "results", "report"}
    assert set(FileResult.__annotations__) == {"file_path", "summary", "issues", "tool_calls_made",
                                               "incomplete", "missing", "model", "files_read"}


# --- PMAT-278: the graph's event stream is a new path out of the run ---------------------------

class _EchoingModel(RecordingModel):
    """Like RecordingModel, but its file summary repeats every tool result it was shown, so a
    leak into the model would also reach the state, the report and the stream."""

    def __call__(self, messages, tools):
        reply = super().__call__(messages, tools)
        if tools is not None and reply.content:
            shown = " | ".join(m["content"] for m in messages if m.get("role") == "tool")
            reply.content = json.dumps({"summary": shown, "issues": []})
        return reply


def _run_recording_stream(capsys, exclusions=None):
    """A full review with every raw stream event recorded; returns (events as text, CLI output)."""
    from langgraph.stream._types import StreamTransformer

    from review_pull_request import graph

    events = []

    class Recorder(StreamTransformer):
        def init(self):
            return {}

        def process(self, event):
            events.append(event)
            return True

    model = _EchoingModel()
    with patch("review_pull_request.graph.STREAM_TRANSFORMERS", (*graph.STREAM_TRANSFORMERS, Recorder)):
        _run_with_model(model, _diff_with_secrets(), exclusions=exclusions, extra_argv=["--verbose"])
    out = capsys.readouterr()
    return json.dumps(events, default=str), out.out + out.err


def _run_with_model(model, diff, extra_argv=(), exclusions=None):
    with patch("tests.test_canary.RecordingModel", lambda: model):
        return _run(diff, extra_argv=extra_argv, exclusions=exclusions)


def test_canary_never_appears_in_a_stream_event_or_the_cli_output(capsys):
    events, output = _run_recording_stream(capsys)

    assert '"method": "tasks"' in events and '"method": "messages"' in events, \
        "the recorder saw no node or model events, so the test proves nothing"
    assert CANARY not in events
    assert CANARY not in output


def test_the_stream_harness_detects_a_leak_when_protection_is_off(capsys):
    events, output = _run_recording_stream(capsys, exclusions=[])

    assert CANARY in events


# ── The judge (PMAT-311): a second external model, so a second path out ──────────────────────────
# Paths into the judge's prompt: the diff, the stored review's findings, the files the agent read,
# the PR description, and the case's own text.

JUDGE_REPLY = json.dumps({"correctness": {"why": "w", "score": 3}, "coverage": {"why": "w", "score": 3},
                          "actionability": {"why": "w", "score": 3}})
LOOKS_LIKE_A_KEY = "sk-" + "canarycanarycanarycanary"
JUDGE_REVIEW = {"files_read": ["src/app.py", ".env", ".claude/settings.json", "config/active.json"], "findings": [
    {"file": "src/app.py", "line": 1, "severity": "low", "summary": "The visible finding.", "suggestion": None},
    {"file": ".env", "line": 1, "severity": "high", "summary": f"TOKEN={CANARY} is exposed", "suggestion": CANARY},
]}
JUDGE_CASE = {"repo": "o/r", "base_sha": "b" * 40, "raised_sha": "a" * 40, "category": "logic",
              "description": f"Adds an app. {GITHUB_TOKEN}",
              "notes": f"From a PR comment: {GITHUB_TOKEN} {LOOKS_LIKE_A_KEY}", "label_reason": LOOKS_LIKE_A_KEY,
              "expected": {"action": "raise_true_bug", "flag_file": "src/app.py", "min_severity": "low"},
              "finding": {"file": "src/app.py", "line": 1, "severity": "low", "summary": f"Claim {GITHUB_TOKEN}"}}


def _judge(exclusions=None):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval" / "promptfoo"))
    import llm_rubric

    cfg = Config(model="judge", api_url="http://x", api_token="judge-secret-token",
                 exclusions=list(SENSITIVE_PATTERNS) if exclusions is None else exclusions)
    seen = []

    def model(messages, tools):
        seen.append(json.dumps(messages))
        return SimpleNamespace(content=JUDGE_REPLY, tool_calls=[])

    with patch.object(llm_rubric, "judge_config", return_value=cfg), \
            patch.object(llm_rubric, "get_diff", return_value=_diff_with_secrets()), \
            patch.object(llm_rubric, "GitHubFileReader", FakeReader), \
            patch.object(llm_rubric, "github_token", return_value=GITHUB_TOKEN), \
            patch.object(llm_rubric, "make_judge_chat_model", lambda meta, **kw: chat_model_factory(model)):
        llm_rubric.judge_review(JUDGE_CASE, JUDGE_REVIEW)
    return seen


def test_the_judge_never_receives_an_excluded_file_by_the_diff_or_a_stored_finding():
    seen = _judge()

    assert seen, "the fake judge was never called, so the test proves nothing"
    assert "x = 1" in seen[0] and "The visible finding." in seen[0], "the allowed content must still reach it"
    assert "src/app.py" in seen[0].split("<files_read>")[1], "the file the agent read must still reach it"
    assert not [m for m in seen if CANARY in m]


def test_the_judge_never_receives_a_token_from_the_case_text():
    text = _judge()[0]

    assert "Claim [REDACTED]" in text, "the case's claim must still reach it, with the token removed"
    assert "Adds an app. [REDACTED]" in text, "the PR description must still reach it, with the token removed"
    assert GITHUB_TOKEN not in text and LOOKS_LIKE_A_KEY not in text


def test_the_judge_harness_detects_a_leak_when_protection_is_off():
    assert any(CANARY in m for m in _judge(exclusions=[]))


# ── The silver labeller (PMAT-326): a third external model, with the judge's inputs ─────────────

def _silver(exclusions=None):
    eval_dir = Path(__file__).resolve().parents[1] / "eval"
    with patch.object(sys, "path", [str(eval_dir / "promptfoo"), str(eval_dir / "scripts"), *sys.path]):
        import llm_rubric
        import silver_labels

    cfg = Config(model="judge", api_url="http://x", api_token="judge-secret-token",
                 exclusions=list(SENSITIVE_PATTERNS) if exclusions is None else exclusions)
    seen = []

    def model(messages, tools):
        seen.append(json.dumps(messages))
        return SimpleNamespace(content=JUDGE_REPLY, tool_calls=[])

    with patch.object(llm_rubric, "judge_config", return_value=cfg), \
            patch.object(llm_rubric, "get_diff", return_value=_diff_with_secrets()), \
            patch.object(llm_rubric, "GitHubFileReader", FakeReader), \
            patch.object(llm_rubric, "github_token", return_value=GITHUB_TOKEN), \
            patch.object(silver_labels, "make_labeller_chat_model", lambda *a, **k: chat_model_factory(model)), \
            patch.dict(os.environ, {"SILVER_CALIBRATION_URL": "http://silver", "SILVER_CALIBRATION_TOKEN": "silver-tok"}):
        silver_labels.label_once({**JUDGE_CASE, "case_id": "R1-a-001"}, JUDGE_REVIEW, "openai/labeller", "high")
    return seen


def test_the_silver_labeller_never_receives_an_excluded_file_by_any_path():
    seen = _silver()

    assert seen, "the fake labeller was never called, so the test proves nothing"
    assert "x = 1" in seen[0] and "The visible finding." in seen[0], "the allowed content must still reach it"
    assert "src/app.py" in seen[0].split("<files_read>")[1], "the file the agent read must still reach it"
    assert not [m for m in seen if CANARY in m]


def test_the_silver_labeller_never_receives_a_token_from_the_case_text():
    text = _silver()[0]

    assert "Adds an app. [REDACTED]" in text, "the PR description must still reach it, with the token removed"
    assert "Claim [REDACTED]" in text
    assert GITHUB_TOKEN not in text and LOOKS_LIKE_A_KEY not in text


def test_the_silver_harness_detects_a_leak_when_protection_is_off():
    assert any(CANARY in m for m in _silver(exclusions=[]))


# --- PMAT-325: the review trace is a new path out of the run ------------------------------------
# Invariant: an excluded file's content and the tokens never reach the trace file, by any path.
# The trace is recorded inside the guard, so it holds what the model was sent: refusals for excluded
# files and [REDACTED] for tokens. With the protection off the same harness must find both.

class _TokenReader(FakeReader):
    """FakeReader, but the allowed file src/app.py holds both tokens."""

    def read_file(self, path, max_lines):
        if path == "src/app.py":
            self.asked.append(path)
            return f"KEY = '{GITHUB_TOKEN}'\nMODEL_KEY = '{STORE_MODEL_TOKEN}'\n"
        return super().read_file(path, max_lines)


class _NoRedaction:
    def __init__(self, secrets):
        pass

    def scrub(self, messages):
        return messages


def _run_traced(tmp_path, exclusions=None, redact=True):
    cfg = Config(model="m", api_url="http://x", api_token=STORE_MODEL_TOKEN,
                 exclusions=list(SENSITIVE_PATTERNS) if exclusions is None else exclusions)
    model, trace_path = RecordingModel(), tmp_path / "trace.jsonl"
    with patch("review_pull_request.review.GitHubFileReader", _TokenReader), \
            patch("review_pull_request.review.get_diff", return_value=_diff_with_secrets()), \
            patch("review_pull_request.session.make_chat_model", chat_model_factory(model)), \
            patch("review_pull_request.session.Redactor", Redactor if redact else _NoRedaction):
        run_review_agent("o", "r", "b" * 40, "abc123", "Adds an app.", config=cfg, token=GITHUB_TOKEN,
                      publish=lambda report: None, all_excluded=lambda paths: 0, trace_path=trace_path)
    return model, trace_path.read_text()


def test_no_excluded_content_or_token_reaches_the_trace(tmp_path):
    model, text = _run_traced(tmp_path)

    assert set(model.requested) == set(READ_ATTEMPTS), "every read attempt must actually be exercised"
    assert '"src/app.py"' in text and "[REDACTED]" in text, "the trace holds no reads, so the scan proves nothing"
    for secret in (CANARY, GITHUB_TOKEN, STORE_MODEL_TOKEN):
        assert secret not in text


def test_the_trace_harness_detects_a_leak_when_protection_is_off(tmp_path):
    _, text = _run_traced(tmp_path, exclusions=[], redact=False)

    for secret in (CANARY, GITHUB_TOKEN, STORE_MODEL_TOKEN):
        assert secret in text
