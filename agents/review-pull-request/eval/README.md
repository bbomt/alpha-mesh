# Eval cases for review-pull-request

A dataset of the review agent's false positives (PMAT-300) and real issues (PMAT-301) on merged PRs,
and the tool that mines it. The dataset is used later to improve the agent: it should stop raising the
false positives while still reporting the real issues. Running the agent against the dataset (scoring)
is separate work (PMAT-304).

## The rules

> **A real-issue case is a high or medium finding from a review comment on a merged PR whose ID a
> commit in that PR cites** (e.g. `fix[PMAT-12]: fixed review finding R31-e0c98f3-003 in src/x.py`).

The worker fixed the finding and said so. The ID (`R<PR>-<reviewed commit, 7 chars>-<NNN>`, PMAT-305) is
on the finding's heading, so the match is exact and needs no judgement. This applies to reviews from
PMAT-305 on; older findings have no ID and are never real-issue cases by rule (one can still be labelled
`real_issue` by hand, see below).

> **A false-positive case is a high or medium finding from a review comment on a merged PR, on a file
> that did not change between the commit that review covered and the PR's final commit.**

The worker saw the finding and merged that exact file anyway, so the finding was dismissed. A finding on
a file that changed afterwards may have been fixed, so it is not a case. Low findings are never cases:
they are often left in place by design.

A citation wins over this rule: a finding fixed somewhere else (say, in its caller) is a real issue even
though its own file never changed.

Mining therefore needs no model and no judgement: only the PR's review comments, commits and git
history. It is repeatable and gives the same cases every time.

## Tool

`scripts/eval_cases.py` is standalone. It never imports or changes the review code, never runs a model,
and never writes to GitHub: `gh` is only used to read PRs, comments, PR commits and commit comparisons.

| Folder | Holds |
| :-- | :-- |
| `scripts/` | the tool (stdlib only) |
| `cases/` | one JSON file per case (committed: the dataset) |
| `tests/` | acceptance tests with a fake `gh` |

```bash
cd agents/review-pull-request
S=eval/scripts/eval_cases.py

# Mine the dataset (needs `gh auth login`)
python3 $S mine --repo bbomt/alpha-mesh --limit 40
python3 $S mine --repo bbomt/alpha-mesh --prs 19,20          # only some PRs

# Override a case when a dismissed finding turns out to be a real bug
python3 $S label <case_id> real_issue --reason "The caller never checks the length (src/x.py:40)."
```

Mining again rewrites the case files from the comments and commits but keeps any label set with
`label`. A label set by a rule is recomputed, so a false positive becomes a real issue once a commit
cites it.

## How `mine` applies the rule

For each merged PR, each review comment (`# AI Code Review` or `# Partial pull request review`) is parsed
for its `Reviewed commit` and its findings, and the PR's commit messages are searched for finding IDs
(only IDs of that PR; GitHub lists at most 250 commits per PR). Each high or medium finding is then:

- **a real-issue case** if a commit cites its ID. `fix_sha` is the first commit that cites it.
- **a false-positive case** if its file is not among the files changed from the reviewed commit to the PR's final
  commit (GitHub's compare API). A comment on the final commit itself always qualifies.
- **skipped as `file_changed`** if its file changed afterwards and no commit cites it (a rename counts as
  a change).
- **skipped as `not_ancestor`** if the reviewed commit is no longer in the PR's history (force-pushed
  or rebased away), or the comparison lists too many files to be sure.
- **skipped as `no_commit`** if the comment records no commit (the old pr-review-performance format,
  PRs #5–#7).

The same finding raised by several reviews (same PR, file and line; the wording may differ) is one case.
`times_raised` counts the reviews that raised it, and `raised_sha` is the first, or for a real issue the
review whose ID was cited. A repeat reported on a
neighbouring line stays a separate case.

GitHub keeps every PR's commits after a squash merge and branch deletion (`refs/pull/N/head`), so the
comparisons work on old PRs.

## Case file

```json
{
  "case_id": "3f2a…", "repo": "bbomt/alpha-mesh", "pr_number": 19,
  "base_sha": "…", "head_sha": "<the PR's final commit>", "raised_sha": "<the commit the review covered>",
  "fix_sha": "<the commit citing the finding's ID, or null>",
  "source": "pr_comment", "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
  "finding": {"file": "…", "line": 115, "severity": "high | medium", "summary": "Problem text",
              "fix": "Fix text or null", "failure_scenario": null, "id": "R19-1a2b3c4-002 or null"},
  "times_raised": 1,
  "label": "false_positive | real_issue",
  "label_by": "rule", "label_date": "2026-10-27",
  "label_reason": "Raised in review and merged with the file unchanged. | Commit 1a2b3c4 in the PR cites finding R31-…"
}
```

- The agent's severities are high/medium/low (there is no INFO).
- `id` is the finding's ID from the review's heading (PMAT-305), from the first review that raised it;
  null for reports written before IDs existed.
- `failure_scenario` is null: the agent writes a Problem and a Fix, not a separate failure scenario.
- For a false positive, `head_sha` is the merged code the case applies to; the finding's file is
  identical there and at `raised_sha`, so the case's line number is valid at both.
- For a real issue, the bug is in the code at `raised_sha` (before `fix_sha`); scoring reviews that
  commit, not `head_sha`, where it is already fixed. `mesh review-pull-request` only reviews a PR's head,
  so scoring (PMAT-304) needs a way to review a given commit.
- The invalid-file-access rates are PMAT-302.
