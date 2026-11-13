"""The gate checklist for one PR (PMAT-312): what `mesh merge-pr` enforces and `mesh eval-gate --check` prints.

GitHub is a fake: a dict from the `gh` call to its JSON answer.
"""

import json

import gate_check

URL = "https://github.com/o/r/pull/7"
HEAD, OLD = "h" * 40, "o" * 40
GATED = "agents/review-pull-request/src/review_pull_request/stage2.py"


def fake(*, files=(GATED,), head_evals=None, audit=None, behind=0, branch="feature/x", commits=(OLD, HEAD),
         old_evals=None, since_old=("README.md",)):
    def status(state, context):
        return {"statuses": [{"context": context, "state": state}] if state else []}
    answers = {
        ("pr", "view"): {"state": "OPEN", "headRefOid": HEAD, "baseRefName": "master", "headRefName": branch},
        ("api", "repos/o/r/compare/master..." + HEAD): {"behind_by": behind},
        ("api", "repos/o/r/commits/master/status"): status(audit, "eval-audit"),
        ("api", f"repos/o/r/commits/{HEAD}/status"): status(head_evals, "evals"),
        ("api", f"repos/o/r/commits/{OLD}/status"): status(old_evals, "evals"),
        ("api", "repos/o/r/pulls/7/commits?per_page=100"): [{"sha": c} for c in commits],
        ("api", f"repos/o/r/compare/{OLD}...{HEAD}"): {"files": [{"filename": f} for f in since_old]},
    }
    calls = []

    def gh(*args):
        calls.append(args)
        if args[:2] == ("pr", "diff"):
            return "\n".join(files)
        return json.dumps(answers[(args[0], args[1] if args[0] == "pr" else args[1])])
    gh.calls = calls
    return gh


def check(**kw):
    gh = fake(**kw)
    return gate_check.check(URL, gh=gh), gh


def test_an_agent_pr_with_evals_on_its_head_may_merge():
    result, _ = check(head_evals="success")
    assert result.ok
    assert "[x] evals on head hhhhhhh: success" in result.text


def test_an_agent_pr_without_evals_may_not_and_the_next_command_is_given():
    result, _ = check()
    assert not result.ok
    assert "[ ] evals on head hhhhhhh: missing" in result.text
    assert f"next: mesh eval-gate {URL}" in result.text


def test_failed_or_pending_evals_block():
    for state in ("failure", "pending", "error"):
        result, _ = check(head_evals=state)
        assert not result.ok and f": {state}" in result.text


def test_a_pass_on_an_older_commit_carries_over_when_no_gated_file_changed_since():
    result, _ = check(old_evals="success", since_old=("README.md", "agents/review-pull-request/tests/test_x.py"))
    assert result.ok
    assert "carried from ooooooo" in result.text


def test_it_does_not_carry_over_when_a_gated_file_changed_since():
    result, _ = check(old_evals="success", since_old=("README.md", GATED))
    assert not result.ok
    assert GATED in result.text


def test_a_pr_that_changes_no_gated_file_needs_no_evals_and_reads_no_status():
    result, gh = check(files=("agents/dream/dream/core.py",))
    assert result.ok
    assert "no eval needed" in result.text
    assert not any("/status" in a[1] and HEAD in a[1] for a in gh.calls if a[0] == "api")


def test_a_red_master_blocks_every_merge():
    result, _ = check(files=("README.md",), audit="failure")
    assert not result.ok
    assert "[ ] master audit: failure" in result.text
    assert "mesh eval-gate --master" in result.text


def test_a_red_master_still_lets_the_revert_pr_through():
    result, _ = check(audit="failure", branch="eval-revert/abc1234", head_evals="success")
    assert result.ok


def test_a_stale_branch_is_shown_unticked():
    result, _ = check(head_evals="success", behind=2)
    assert not result.ok
    assert "[ ] up to date with master: 2 commit(s) behind" in result.text


def test_a_red_master_lets_a_gated_pr_with_a_passing_gate_through():
    result, _ = check(audit="failure", head_evals="success")
    assert result.ok and "recovers it" in result.text


def test_a_red_master_blocks_a_gated_pr_whose_gate_has_not_passed():
    result, _ = check(audit="failure")
    assert not result.ok and "mesh eval-gate --master" in result.text
