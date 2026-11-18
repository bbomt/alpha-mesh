"""AT2 (PMAT-312): which changed files need the eval gate before `mesh merge-pr` merges."""

import subprocess
import sys

import pytest

import list_evals_gated_files

A = "agents/review-pull-request/"


@pytest.mark.parametrize("path", [
    A + "src/review_pull_request/stage2.py",
    A + "src/review_pull_request/prompts/per_file.md",
    A + "review_config.toml",
    A + "pyproject.toml",
    A + "uv.lock",
    A + "eval/promptfoo/case_rules.py",
    A + "eval/promptfooconfig.yaml",
    A + "eval/cases/synthetic/sec-001.json",
    A + "eval/holdout.json",
])
def test_a_file_that_can_change_the_findings_or_their_score_is_a_trigger(path):
    assert list_evals_gated_files.triggers([path]) == [path]


@pytest.mark.parametrize("path", [
    "agents/dream/dream/core.py",
    "agents/orchestrator/src/orchestrator/cli.py",
    "bin/merge-pr.sh",
    "README.md",
    A + "README.md",
    A + "eval/README.md",
    A + "tests/test_review.py",
    A + "eval/tests/test_gate.py",
    A + "eval/scripts/score_eval_run.py",
    A + "eval/judge/rubric.v1.md",
    A + "eval/baseline.json",
    "other/agents/review-pull-request/src/x.py",
])
def test_a_file_that_cannot_is_not(path):
    assert list_evals_gated_files.triggers([path]) == []


def test_the_command_line_prints_only_the_triggers():
    out = subprocess.run([sys.executable, list_evals_gated_files.__file__], input=f"README.md\n{A}uv.lock\n\n",
                         capture_output=True, text=True, check=True).stdout
    assert out.split() == [A + "uv.lock"]
