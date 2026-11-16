"""Find ungated merges (PMAT-312): did every agent change on master pass `evals` before it landed?

    python3 eval/scripts/find_ungated_merges.py <owner/repo> <sha> [--dry-run]

The repo is private on GitHub's free plan, so a manual merge cannot be blocked; it is caught here instead, on every
push to master (.github/workflows/find-ungated-merges.yml). The verdict goes on the commit as the status `master-evals-gated`:

- **success** when the commit came from a PR whose head has `evals` = success (or carries it from an older commit)
  and that head holds master's previous commit: the gate then ran on the whole resulting code, so master is green
  again even after an earlier red commit;
- **failure** when the commit changes a gated file (list_evals_gated_files.py) without that: a manual merge, a merge of a
  failing or stale gate, or a push without a PR. The PR gets a comment;
- otherwise the parent's colour: red stays red until a passing gate (`mesh run-evals --master`, or a gated PR) or
  the revert lands. A parent with no master-evals-gated status (before the gate existed) counts as green.

While master's head is red, `mesh merge-pr` refuses every merge but the revert PR (check_evals_gate_allows_merge.py). Exits 1 on
failure, so the workflow run fails and GitHub emails the owner. Standard library only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_evals_gate_allows_merge  # noqa: E402
import list_evals_gated_files  # noqa: E402

CONTEXT = "master-evals-gated"


def audit(repo: str, sha: str, gh=check_evals_gate_allows_merge._gh, post: bool = True) -> tuple[str, str]:
    commit = json.loads(gh("api", f"repos/{repo}/commits/{sha}"))
    parent = commit["parents"][0]["sha"] if commit.get("parents") else None
    gated = list_evals_gated_files.triggers(f["filename"] for f in commit.get("files", []))
    prs = json.loads(gh("api", f"repos/{repo}/commits/{sha}/pulls"))
    pr = next((p for p in prs if p.get("merge_commit_sha") == sha), prs[0] if prs else None)
    comment = None

    passed = False
    if pr:
        head = pr["head"]["sha"]
        evals = check_evals_gate_allows_merge._status(gh, repo, head, "evals")[0]
        gate_ok = evals == "success" or bool(check_evals_gate_allows_merge._carried(gh, repo, str(pr["number"]), head))
        holds_parent = parent is None or json.loads(gh("api", f"repos/{repo}/compare/{parent}...{head}"))["behind_by"] == 0
        passed = gate_ok and holds_parent
    if passed:
        state, desc = "success", f"PR #{pr['number']} passed the eval gate on the whole code"
    elif gated and pr:
        state, desc = "failure", f"ungated agent change: PR #{pr['number']} merged without a passing eval gate"
        comment = (f"**Master is red (master-evals-gated).** This PR changed files the review-pull-request eval gate covers "
                   f"({', '.join(gated[:5])}{', …' if len(gated) > 5 else ''}) and was merged as `{sha[:7]}` without "
                   "a passing `mesh run-evals` on a head holding master's previous commit. `mesh merge-pr` now refuses "
                   "every other merge. Recover with `mesh run-evals --master`: if the gate passes, master turns green; "
                   "if it fails, it opens the revert PR.")
    elif gated:
        state, desc = "failure", f"ungated agent change: {sha[:7]} pushed without a PR"
    else:
        before = check_evals_gate_allows_merge._status(gh, repo, parent, CONTEXT)[0] if parent else "missing"
        if before in ("failure", "error"):
            state, desc = "failure", f"inherits red from {parent[:7]}: an earlier ungated agent change"
        else:
            state, desc = "success", "no gated file changed"

    if post:
        gh("api", f"repos/{repo}/statuses/{sha}", "-f", f"state={state}", "-f", f"context={CONTEXT}",
           "-f", f"description={desc[:140]}")
        if comment:
            gh("pr", "comment", str(pr["number"]), "--repo", repo, "--body", comment)
    return state, desc


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    state, desc = audit(args[0], args[1], post="--dry-run" not in sys.argv)
    print(f"master-evals-gated {args[1][:7]}: {state}: {desc}")
    sys.exit(0 if state == "success" else 1)
