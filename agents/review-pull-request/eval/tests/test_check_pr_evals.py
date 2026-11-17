"""The merge status report for one PR (PMAT-312): what `mesh merge-pr` enforces and the `evals-gate` job runs.

GitHub is a fake: a dict from the `gh` call to its JSON answer.
"""

import json

import check_pr_evals

URL = "https://github.com/o/r/pull/7"
HEAD, OLD = "h" * 40, "o" * 40
GATED = "agents/review-pull-request/src/review_pull_request/stage2.py"


def fake(*, files=(GATED,), head_evals=None, behind=0, commits=(OLD, HEAD),
         old_evals=None, since_old=("README.md",)):
    def status(state, context):
        desc = "runs passed 142/195 · needs at least 130/195" if state == "success" else ""
        return {"statuses": [{"context": context, "state": state, "description": desc}] if state else []}
    answers = {
        ("pr", "view"): {"state": "OPEN", "headRefOid": HEAD, "baseRefName": "master"},
        ("api", "repos/o/r/compare/master..." + HEAD): {"behind_by": behind},
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


def check(evals_only=False, **kw):
    gh = fake(**kw)
    return check_pr_evals.check(URL, gh=gh, evals_only=evals_only), gh


def test_an_agent_pr_with_evals_on_its_head_may_merge():
    result, _ = check(head_evals="success")
    assert result.ok
    assert result.text.startswith("Merge status for PR #7 (head hhhhhhh): READY TO MERGE")
    assert "  OK       evals passed on head hhhhhhh: runs passed 142/195 · needs at least 130/195" in result.text
    assert "  OK       it changes 1 agent file(s), so it needs a passing evals status" in result.text
    assert "Next:" not in result.text


def test_an_agent_pr_without_evals_may_not_and_the_next_command_is_given():
    result, _ = check()
    assert not result.ok
    assert "): BLOCKED by 1 item\n" in result.text
    assert "  BLOCKED  evals have not run on head hhhhhhh" in result.text
    assert result.text.endswith(f"\n\nNext: mesh run-evals {URL}")


def test_failed_or_pending_evals_block():
    for state in ("failure", "pending", "error"):
        result, _ = check(head_evals=state)
        assert not result.ok and "  BLOCKED  evals " in result.text


def test_a_pass_on_an_older_commit_carries_over_when_no_gated_file_changed_since():
    result, _ = check(old_evals="success", since_old=("README.md", "agents/review-pull-request/tests/test_x.py"))
    assert result.ok
    assert "  OK       evals passed on ooooooo, an older commit of this PR, and no agent file changed since" in result.text


def test_it_does_not_carry_over_when_a_gated_file_changed_since():
    result, _ = check(old_evals="success", since_old=("README.md", GATED))
    assert not result.ok
    assert GATED in result.text


def test_a_pr_that_changes_no_gated_file_needs_no_evals_and_reads_no_status():
    result, gh = check(files=("agents/dream/dream/core.py",))
    assert result.ok
    assert "  OK       it changes no agent file, so it needs no evals" in result.text
    assert not any("/status" in a[1] and HEAD in a[1] for a in gh.calls if a[0] == "api")




def test_a_stale_branch_is_shown_blocked():
    result, _ = check(head_evals="success", behind=2)
    assert not result.ok
    assert "  BLOCKED  it is 2 commit(s) behind master" in result.text


def test_evals_only_is_the_jobs_report_without_the_items_github_checks_itself():
    result, gh = check(evals_only=True, head_evals="success", behind=3)
    assert result.ok  # a stale branch is GitHub's own rule to enforce, not the evals check's
    assert "the PR is open" not in result.text and "behind" not in result.text
    assert not any(a[1].startswith("repos/o/r/compare/master...") for a in gh.calls if a[0] == "api")
    result, _ = check(evals_only=True)
    assert not result.ok and f"Next: mesh run-evals {URL}" in result.text
