"""Acceptance tests for resumable reviews (spec PMAT-237 5.1): the CLI in a subprocess, killed and rerun.

Each run is tests/fixtures/run_review.py: the real review.main() with a SqliteSaver file (survives
kill -9), a fake model and fake GitHub that log every model call and posted comment.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from review_pull_request.constants import REVIEW_SIGNATURE

AGENT = Path(__file__).resolve().parents[1]
RUNNER = AGENT / "tests" / "fixtures" / "run_review.py"
URL = "https://github.com/owner/repo/pull/42"
FILES = ["src/f1.py", "src/f2.py", "src/f3.py", "src/f4.py"]


def make_diff(files) -> str:
    return "".join(
        f"diff --git a/{f} b/{f}\n--- a/{f}\n+++ b/{f}\n@@ -1 +1,2 @@\n+line one of {f}\n"
        for f in files
    )


class Review:
    """One work dir: the checkpoint file, fake GitHub's posts and the fake model's call log."""

    def __init__(self, tmp_path: Path):
        self.dir = tmp_path
        (tmp_path / "diff.txt").write_text(make_diff(FILES))
        self.env = {**os.environ, "HOME": str(tmp_path), "RR_DIR": str(tmp_path),
                    "RR_DB": str(tmp_path / "checkpoints.sqlite"), "PYTHONPATH": str(AGENT)}

    def start(self, *args, **env) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, str(RUNNER), URL, *args], cwd=AGENT,
                                env={**self.env, **env}, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def run(self, *args, **env) -> subprocess.CompletedProcess:
        p = self.start(*args, **env)
        out, err = p.communicate(timeout=60)
        return subprocess.CompletedProcess(p.args, p.returncode, out, err)

    def calls(self) -> list[dict]:
        path = self.dir / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def posts(self) -> list[str]:
        path = self.dir / "posts.jsonl"
        return [json.loads(line)["body"] for line in path.read_text().splitlines()] if path.exists() else []

    def wait_for_call(self, proc, **match):
        deadline = time.time() + 30
        while time.time() < deadline:
            if any(all(c.get(k) == v for k, v in match.items()) for c in self.calls()):
                return
            assert proc.poll() is None, f"run ended early: {proc.communicate()}"
            time.sleep(0.05)
        raise AssertionError(f"no model call matching {match}")

    def kill_while_hung(self, hang: str, sig=signal.SIGKILL, *args, **env):
        """Start a run whose model hangs on `hang` (a file or "overview"), wait for that call, kill it."""
        proc = self.start(*args, RR_HANG=hang, **env)
        if hang == "overview":
            self.wait_for_call(proc, kind="overview")
        else:
            self.wait_for_call(proc, kind="file", file=hang)
        proc.send_signal(sig)
        proc.communicate(timeout=30)
        assert proc.returncode != 0
        return proc


def file_calls(calls):
    return [c["file"] for c in calls if c["kind"] == "file"]


def assert_each_file_once(report: str, files=FILES):
    for f in files:
        assert report.count(f"finding-in-{f}") == 1, f


# --- A1, A2, A17: SIGKILL while a file is in flight, resumed at once -----------------------------

@pytest.mark.parametrize("k", [1, 3])
def test_a1_a2_a17_sigkill_on_file_k_resumes_from_k(tmp_path, k):
    r = Review(tmp_path)
    r.kill_while_hung(FILES[k - 1])
    before = len(r.calls())

    resumed = r.run()

    assert resumed.returncode == 0, resumed.stderr
    assert "another review is running" not in resumed.stderr  # A17: the OS released the lock
    new = r.calls()[before:]
    assert not [c for c in new if c["kind"] == "overview"]
    assert file_calls(new) == FILES[k - 1:]  # files 1..K-1 not re-sent
    [report] = r.posts()
    assert_each_file_once(report)


# --- A3: SIGINT and SIGTERM ------------------------------------------------------------------------

@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_a3_sigint_and_sigterm_leave_no_partial_result(tmp_path, sig):
    r = Review(tmp_path)
    r.kill_while_hung("src/f2.py", sig)
    assert r.posts() == []
    before = len(r.calls())

    resumed = r.run()

    assert resumed.returncode == 0, resumed.stderr
    assert file_calls(r.calls()[before:]) == FILES[1:]  # f2 had no result, so it is reviewed again
    assert_each_file_once(r.posts()[0])


# --- A4: hung overview -----------------------------------------------------------------------------

def test_a4_hung_overview_is_rerun_on_resume(tmp_path):
    r = Review(tmp_path)
    r.kill_while_hung("overview")
    assert r.posts() == []
    before = len(r.calls())

    resumed = r.run()

    assert resumed.returncode == 0, resumed.stderr
    new = r.calls()[before:]
    assert [c["kind"] for c in new].count("overview") == 1
    assert file_calls(new) == FILES


# --- A5: model error on file K ---------------------------------------------------------------------

def test_a5_model_error_fails_noisily_and_resume_keeps_earlier_findings(tmp_path):
    r = Review(tmp_path)
    failed = r.run(RR_FAIL="src/f3.py")
    assert failed.returncode == 1
    assert "LLM API call failed during src/f3.py" in failed.stderr
    assert "Completed work is kept: overview + 2 file(s)" in failed.stderr
    assert r.posts() == []
    before = len(r.calls())

    resumed = r.run()

    assert resumed.returncode == 0, resumed.stderr
    assert file_calls(r.calls()[before:]) == ["src/f3.py", "src/f4.py"]
    report = r.posts()[0]
    assert "finding-in-src/f1.py" in report and "finding-in-src/f2.py" in report
    assert_each_file_once(report)


# --- A6: resume with a different model -------------------------------------------------------------

def test_a6_resume_with_a_different_model(tmp_path):
    r = Review(tmp_path)
    r.kill_while_hung("src/f3.py")
    before = len(r.calls())

    resumed = r.run("--model", "fake/model-b")

    assert resumed.returncode == 0, resumed.stderr
    new = r.calls()[before:]
    assert {c["model"] for c in new} == {"fake/model-b"}
    assert file_calls(new) == ["src/f3.py", "src/f4.py"]
    report = r.posts()[0]
    assert "**Models:** `fake/model-a` · `fake/model-b`" in report
    assert "#### `src/f1.py` · line 1 · model: `fake/model-a`" in report
    assert "#### `src/f2.py` · line 1 · model: `fake/model-a`" in report
    assert "#### `src/f3.py` · line 1 · model: `fake/model-b`" in report
    assert "#### `src/f4.py` · line 1 · model: `fake/model-b`" in report


# --- A7, A8: a new head SHA or exclusion policy is a new thread --------------------------------------

@pytest.mark.parametrize("change", [{"RR_HEAD": "head222"}, {"RR_EXCLUDE": "*.md"}], ids=["A7-head", "A8-policy"])
def test_a7_a8_changed_key_reuses_nothing(tmp_path, change):
    r = Review(tmp_path)
    assert r.run().returncode == 0
    before = len(r.calls())

    again = r.run(**change)

    assert again.returncode == 0, again.stderr
    assert "Starting review" in again.stderr
    new = r.calls()[before:]
    assert [c["kind"] for c in new].count("overview") == 1
    assert file_calls(new) == FILES


# --- A10: completed thread run again ---------------------------------------------------------------

def test_a10_completed_thread_is_skipped(tmp_path):
    r = Review(tmp_path)
    first = r.run()
    assert first.returncode == 0
    before = len(r.calls())

    again = r.run()

    assert again.returncode == 0, again.stderr
    assert "Already completed (thread review-pull-request:owner/repo#42:" in again.stderr
    assert len(r.calls()) == before
    assert len(r.posts()) == 1
    assert r.posts()[0] in again.stdout  # the stored report is printed


# --- A12: --since-last-review killed and resumed ----------------------------------------------------

def test_a12_incremental_review_resumes_on_its_own_thread(tmp_path):
    r = Review(tmp_path)
    base = "ba5e" + "0" * 36
    (tmp_path / "posts.jsonl").write_text(json.dumps(
        {"body": f"{REVIEW_SIGNATURE} (full review of {base[:7]})\n\n> **Reviewed commit:** `{base}`\n"}) + "\n")
    inc = FILES[:3]
    (tmp_path / "inc_diff.txt").write_text(make_diff(inc))
    r.kill_while_hung("src/f2.py", signal.SIGKILL, "--since-last-review", RR_HEAD="head222")
    before = len(r.calls())

    resumed = r.run("--since-last-review", RR_HEAD="head222")

    assert resumed.returncode == 0, resumed.stderr
    assert f":since-{base}:" in resumed.stderr
    assert file_calls(r.calls()[before:]) == ["src/f2.py", "src/f3.py"]
    report = r.posts()[-1]
    assert "since last review" in report
    assert_each_file_once(report, inc)


# --- A13: --force-rerun ----------------------------------------------------------------------------

def test_a13_force_rerun_discards_the_thread(tmp_path):
    r = Review(tmp_path)
    assert r.run().returncode == 0
    before = len(r.calls())

    again = r.run("--force-rerun")

    assert again.returncode == 0, again.stderr
    new = r.calls()[before:]
    assert [c["kind"] for c in new].count("overview") == 1
    assert file_calls(new) == FILES
    assert len(r.posts()) == 2


# --- A14: resume console output --------------------------------------------------------------------

def test_a14_resume_lists_files_from_checkpoint_and_files_reviewed_now(tmp_path):
    r = Review(tmp_path)
    r.kill_while_hung("src/f3.py")

    resumed = r.run()

    err = resumed.stderr
    assert "Resuming: 2 file(s) from checkpoint, 2 to review" in err
    assert "  From checkpoint: src/f1.py, src/f2.py\n" in err
    assert "  To review now: src/f3.py, src/f4.py\n" in err


# --- A16: a second process on a running thread -----------------------------------------------------

def test_a16_second_process_on_a_running_thread_exits(tmp_path):
    r = Review(tmp_path)
    first = r.start(RR_HANG="src/f2.py")
    try:
        r.wait_for_call(first, kind="file", file="src/f2.py")
        before = len(r.calls())

        second = r.run()

        assert second.returncode == 1
        assert f"another review is running on this thread (pid {first.pid})" in second.stderr
        assert len(r.calls()) == before  # nothing reviewed
        assert first.poll() is None  # first run unaffected
    finally:
        first.kill()
        first.communicate(timeout=30)


# --- The diff changed under the same thread (e.g. the PR was retargeted to another base) ------------

def test_resume_refuses_when_the_diff_no_longer_matches_the_saved_files(tmp_path):
    r = Review(tmp_path)
    r.kill_while_hung("src/f3.py")
    (tmp_path / "diff.txt").write_text(make_diff(["src/f1.py", "src/f2.py", "src/f5.py"]))
    before = len(r.calls())

    resumed = r.run()

    assert resumed.returncode == 1
    assert "the PR's changed files differ from the saved review" in resumed.stderr
    assert "--force-rerun" in resumed.stderr
    assert len(r.calls()) == before  # nothing reviewed
    assert r.posts() == []
