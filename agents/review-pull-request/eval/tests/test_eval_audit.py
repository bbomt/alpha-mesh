"""AT6 (PMAT-312): the audit of a master commit. GitHub is a fake: the `gh` call to its JSON answer, and a log of the
statuses and comments it would post."""

import json

import eval_audit

REPO, C, P, HEAD = "o/r", "c" * 40, "p" * 40, "h" * 40
GATED = "agents/review-pull-request/src/review_pull_request/stage2.py"


def fake(*, files=(GATED,), pr=True, head_evals=None, behind=0, parent_audit=None):
    def status(state, context):
        return {"statuses": [{"context": context, "state": state}] if state else []}
    answers = {
        f"repos/{REPO}/commits/{C}": {"sha": C, "parents": [{"sha": P}], "files": [{"filename": f} for f in files]},
        f"repos/{REPO}/commits/{C}/pulls": ([{"number": 9, "merge_commit_sha": C, "head": {"sha": HEAD}}] if pr else []),
        f"repos/{REPO}/commits/{HEAD}/status": status(head_evals, "evals"),
        f"repos/{REPO}/commits/{P}/status": status(parent_audit, "eval-audit"),
        f"repos/{REPO}/compare/{P}...{HEAD}": {"behind_by": behind},
        f"repos/{REPO}/pulls/9/commits?per_page=100": [{"sha": HEAD}],
    }
    posted = []

    def gh(*args):
        if args[0] == "api" and "-f" in args:
            posted.append(("status", args[1], dict(a.split("=", 1) for a in args[2:] if "=" in a)))
            return "{}"
        if args[:2] == ("pr", "comment"):
            posted.append(("comment", args[2], args[-1]))
            return ""
        return json.dumps(answers[args[1]])
    gh.posted = posted
    return gh


def audit(**kw):
    gh = fake(**kw)
    return eval_audit.audit(REPO, C, gh=gh), gh


def test_a_gated_pr_that_passed_the_gate_on_masters_tip_turns_master_green():
    (state, desc), gh = audit(head_evals="success", parent_audit="failure")
    assert state == "success" and "#9" in desc
    assert gh.posted[0] == ("status", f"repos/{REPO}/statuses/{C}",
                            {"state": "success", "context": "eval-audit", "description": desc})


def test_a_gated_pr_merged_without_a_passing_gate_turns_master_red_and_says_so_on_the_pr():
    (state, desc), gh = audit(head_evals=None)
    assert state == "failure" and "ungated" in desc and "#9" in desc
    assert any(p[0] == "comment" and "mesh eval-gate --master" in p[2] for p in gh.posted)


def test_a_gate_run_on_a_branch_without_masters_previous_commit_does_not_count():
    (state, desc), _ = audit(head_evals="success", behind=1)
    assert state == "failure" and "ungated" in desc


def test_a_gated_change_pushed_without_a_pr_is_red():
    (state, desc), _ = audit(pr=False)
    assert state == "failure" and "without a PR" in desc


def test_an_overridden_gate_counts_as_gated():
    (state, _), _ = audit(head_evals="success")  # an override is evals = success, posted by mesh eval-gate --override
    assert state == "success"


def test_a_commit_that_changes_no_gated_file_inherits_its_parents_colour():
    (state, desc), _ = audit(files=("README.md",), parent_audit="failure")
    assert state == "failure" and "inherits" in desc and P[:7] in desc
    (state, _), _ = audit(files=("README.md",), parent_audit=None)  # no audit yet: before the gate existed
    assert state == "success"


def test_dry_run_posts_nothing():
    gh = fake(head_evals=None)
    eval_audit.audit(REPO, C, gh=gh, post=False)
    assert gh.posted == []
