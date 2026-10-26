# Eval cases for review-pull-request

A dataset of the review's findings on merged PRs, labelled by a judge, and the harness that builds it
(PMAT-300). A merged PR is treated as clean, so every high/medium finding on it is a *candidate* false
positive until it is labelled.

`scripts/eval_cases.py` is a standalone data generator. It runs `mesh review-pull-request` as a user
would and parses the report it prints. It never imports or changes the review code and never writes to
GitHub: reviews run with `--no-post-comment` (not `--dry-run`, which swaps in a scripted fake model), and
`gh` is only used to list PRs and read their comments.

| Folder | Holds |
| :-- | :-- |
| `scripts/` | the harness (stdlib only) |
| `cases/` | one JSON file per case (committed: the dataset) |
| `reports/` | `eval_report_YYYYMMDD.json` run reports (gitignored) |
| `tests/` | acceptance tests with a fake `gh` and a fake `mesh` |

## Prerequisites

As for `mesh review-pull-request`: `gh auth login`, the `CODE_REVIEW_*` settings in `alpha-mesh/.env`,
and AWS access for the checkpoint store. `mesh` must be on `PATH`. Use the free Nemotron models
(`alpha-mesh/CLAUDE_ENGINEERING.md`); the paid model only when asked.

## Workflow

```bash
cd agents/review-pull-request
S=eval/scripts/eval_cases.py

# 1. Mine: cases from the review comments on each PR, plus a fresh review of each PR (scored)
python3 $S mine --repo bbomt/alpha-mesh --limit 20 --model nvidia/nemotron-3-ultra-550b-a55b:free
python3 $S mine --repo bbomt/alpha-mesh --prs 12,16 --model nvidia/nemotron-3.5-super-vl:free  # redo some PRs

# 2. Re-run: review each case's PR again with the model that raised the case
python3 $S rerun
python3 $S rerun --retry-not-reproduced    # once more, since model output varies

# 3. Judge: check each reproduced case against the code at its head_sha, then record the verdict
python3 $S label <case_id> false_positive --reason "The caller checks the length (src/x.py:40)."
python3 $S label <case_id> real_issue --reason "..."

# 4. Rates from the judged cases
python3 $S report eval/reports/eval_report_20261026.json
```

A review that throttles or fails leaves no report: the PR is recorded with `"error": true`, left out of
the rates, and can be redone with `--prs` and another model. Each review runs with `--force-rerun`,
because the checkpoint thread is keyed on the PR's head commit, not on the model.

## Case file

```json
{
  "case_id": "3f2a…", "repo": "bbomt/alpha-mesh", "pr_number": 19,
  "base_sha": "…", "head_sha": "…",
  "source": "pr_comment | fresh_run",
  "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
  "finding": {"file": "…", "line": 108, "severity": "high | medium", "summary": "Problem text",
              "fix": "Fix text or null", "failure_scenario": null},
  "label": "unlabelled | false_positive | real_issue | not_reproduced",
  "label_by": null, "label_date": null, "label_reason": null,
  "rerun_model": null, "rerun_sha": null, "rerun_reproduced": null, "rerun_finding": null
}
```

- `head_sha` is the commit the finding was raised on: the `Reviewed commit` of a PR comment, or the PR's
  head for a fresh run. `base_sha` is the PR's base. Keep SHAs, not diffs: GitHub keeps
  `refs/pull/N/head` after the merge.
- The agent's severities are high/medium/low (there is no INFO). Only high and medium become cases.
  `failure_scenario` is null: the agent writes a Problem and a Fix, not a separate failure scenario.
- `case_id` is a hash of repo, PR, commit, source, file, line, severity and summary. Mining again never
  overwrites a case already on disk, so labels survive.
- Review comments that do not record their commit (the old pr-review-performance format, PRs #5–#7)
  are skipped and counted in the report (`comments_skipped_no_commit`).

## Re-runs and labels

- A re-run **reproduces** a case if it has a high or medium finding in the same file within ±5 lines
  (or both have no line). The matching finding is saved in `rerun_finding` for the judge.
- A case that does not reproduce is labelled `not_reproduced` and kept out of the rates.
- **Limitation:** `mesh review-pull-request` always reviews the PR's final head, so a case mined from a
  comment on an earlier commit is re-run at the final head (`rerun_sha`). If it does not reproduce,
  `label_reason` notes that it may have been fixed before the merge. Judge such cases at their own
  `head_sha`.
- `real_issue` cases become true-positive cases for PMAT-301. A real issue still present on master goes
  into the single Jira ticket that tracks bugs found by evals (one ticket, never one per bug).
- Where a PMAT-269 review-quality log entry matches a case, copy its verdict into `label_reason`.

## Report

```json
{
  "repo": "…", "run_date": "2026-10-26", "model": "…", "comments_skipped_no_commit": 3,
  "prs": [{"pr_number": 19, "passed": false, "false_positives": 2, "case_ids": ["…"], "error": false}],
  "totals": {"prs_tested": 18, "prs_failed": 7, "prs_errored": 0, "false_positive_rate": 0.39},
  "labelled": {"prs_with_false_positive": [19], "false_positive_rate": 0.06,
               "label_counts": {"unlabelled": 0, "false_positive": 3, "real_issue": 1, "not_reproduced": 9}}
}
```

- `totals.false_positive_rate`: PRs whose fresh review has any high/medium finding, over PRs tested.
- `labelled.false_positive_rate`: PRs with at least one fresh-run case labelled `false_positive`, over
  PRs tested. Unlabelled, `not_reproduced` and `real_issue` cases never count. `report` recomputes it
  after labelling.
- The invalid-file-access rates are PMAT-302.
