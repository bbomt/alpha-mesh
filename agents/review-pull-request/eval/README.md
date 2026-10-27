# Eval cases for review-pull-request

A dataset of the review agent's false positives on merged PRs, and the tool that builds it (PMAT-300).
The dataset is used later to improve the agent: it should stop raising these findings, while still
reporting the real issues collected by PMAT-301.

## The rule

> **A false-positive case is a high or medium finding from a review comment on a merged PR, on a file
> that did not change between the commit that review covered and the PR's final commit.**

The worker saw the finding and merged that exact file anyway, so the finding was dismissed. A finding on
a file that changed afterwards may have been fixed, so it is not a case.

Mining therefore needs no model and no judgement: only the PR's review comments and git history. It is
repeatable and gives the same cases every time.

## Tool

`scripts/eval_cases.py` is standalone. It never imports or changes the review code and never writes to
GitHub: `gh` is only used to read PRs, comments and commit comparisons.

| Folder | Holds |
| :-- | :-- |
| `scripts/` | the tool (stdlib only) |
| `cases/` | one JSON file per case (committed: the dataset) |
| `reports/` | `eval_report_YYYYMMDD.json` score reports (gitignored) |
| `tests/` | acceptance tests with a fake `gh` and a fake `mesh` |

```bash
cd agents/review-pull-request
S=eval/scripts/eval_cases.py

# Build the dataset (no model calls; needs `gh auth login`)
python3 $S mine --repo bbomt/alpha-mesh --limit 40
python3 $S mine --repo bbomt/alpha-mesh --prs 19,20          # only some PRs

# Override a case when a dismissed finding turns out to be a real bug (it then belongs to PMAT-301)
python3 $S label <case_id> real_issue --reason "The caller never checks the length (src/x.py:40)."

# Score the agent: a full review of each PR, never posted (needs the review's .env and AWS access)
python3 $S score --repo bbomt/alpha-mesh --limit 12 --model nvidia/nemotron-3-ultra-550b-a55b:free
```

Mining again rewrites the case files from the comments but keeps any label set with `label`.

## How `mine` applies the rule

For each merged PR, each review comment (`# AI Code Review` or `# Partial pull request review`) is parsed
for its `Reviewed commit` and its findings. Each high or medium finding is then:

- **a case** if its file is not among the files changed from the reviewed commit to the PR's final
  commit (GitHub's compare API). A comment on the final commit itself always qualifies.
- **skipped as `file_changed`** if its file changed afterwards (a rename counts as a change).
- **skipped as `not_ancestor`** if the reviewed commit is no longer in the PR's history (force-pushed
  or rebased away), or the comparison lists too many files to be sure.
- **skipped as `no_commit`** if the comment records no commit (the old pr-review-performance format,
  PRs #5–#7).

The same finding raised by several reviews (same PR, file and line; the wording may differ) is one case.
`times_raised` counts the reviews that raised it, and `raised_sha` is the first.

GitHub keeps every PR's commits after a squash merge and branch deletion (`refs/pull/N/head`), so the
comparisons work on old PRs.

## Case file

```json
{
  "case_id": "3f2a…", "repo": "bbomt/alpha-mesh", "pr_number": 19,
  "base_sha": "…", "head_sha": "<the PR's final commit>", "raised_sha": "<the commit the review covered>",
  "source": "pr_comment", "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
  "finding": {"file": "…", "line": 115, "severity": "high | medium", "summary": "Problem text",
              "fix": "Fix text or null", "failure_scenario": null},
  "times_raised": 1,
  "label": "false_positive | real_issue",
  "label_by": "rule", "label_date": "2026-10-27",
  "label_reason": "Raised in review and merged with the file unchanged."
}
```

- The agent's severities are high/medium/low (there is no INFO). Only high and medium become cases.
- `failure_scenario` is null: the agent writes a Problem and a Fix, not a separate failure scenario.
- `head_sha` is the merged code the case applies to; the finding's file is identical there and at
  `raised_sha`, so the case's line number is valid at both.

## Score report

`score` runs `mesh review-pull-request <PR> --no-post-comment --force-rerun --model <m>` on each PR (not
`--dry-run`, which swaps in a scripted fake model) and parses the printed report. `--force-rerun` is
needed because the review's checkpoint is keyed on the PR's commit, not on the model.

```json
{
  "repo": "…", "run_date": "2026-10-27", "model": "…",
  "prs": [{"pr_number": 19, "passed": false, "false_positives": 2, "findings": ["…"], "error": false}],
  "totals": {"prs_tested": 12, "prs_failed": 3, "prs_errored": 0, "false_positive_rate": 0.25}
}
```

- False Positive Rate = PRs whose full review has any high or medium finding / PRs reviewed.
- A review that fails or throttles leaves no report: the PR is recorded with `"error": true`, left out
  of the rate, and can be redone with `--prs` and another model.
- Checking whether each case's finding is raised again needs a way to match findings across runs; that
  belongs to the later work that runs these evals against agent changes.
- The invalid-file-access rates are PMAT-302.
