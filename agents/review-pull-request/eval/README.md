# Eval cases for review-pull-request

A dataset of the review agent's false positives and real issues on merged PRs (PMAT-300, PMAT-301, PMAT-307),
and the tool that mines it. The dataset is used later to improve the agent: it should stop raising the
false positives while still reporting the real issues. Running the agent against the dataset (scoring)
is separate work (PMA## The rules

Every high or medium finding has an ID (`R<PR>-<reviewed commit, 7 chars>-<NNN>`, PMAT-305) on its
heading. A case is labelled by what happened to that ID (PMAT-307):

> **Real issue:** a commit in the PR cites the ID (e.g.
> `fix[PMAT-12]: fixed review finding R31-e0c98f3-003 in src/x.py`).

> **False positive:** a comment on the PR dismisses the ID on a line of its own:
> `Dismissed review finding R31-e0c98f3-004: <reason>`. The reason becomes the case's `label_reason`.

Both conventions are part of the review-loop rules (`architecture/CLAUDE_ENGINEERING.md`,
`CLAUDE_GLOBAL.md`). A missing citation is not proof that a finding is false, so a false positive needs
its own explicit signal. Every other finding gives no case:

- **duplicate:** dismissed with a reason that starts `duplicate of <ID>`. The original ID is the case.
- **conflict:** both cited and dismissed. Neither label is certain.
- **unresolved:** neither cited nor dismissed. The count shows how well the conventions are followed.
- **no_id:** a finding without an ID (reviews before PMAT-305).

Low findings are never cases, even when dismissed: they are often left in place by design. Mining needs
no model and no judgement, only the PR's review comments and commits. It gives the same cases every
time.

### The old file rule (PMAT-300, removed)

PMAT-300 labelled a finding a false positive when its file did not change between the reviewed commit and
the PR's final commit. That was a guess, not a signal, and was removed in PMAT-307. Its 49 cases (PRs
#8–#31, all `false_positive`, no IDs) are kept frozen in `cases/legacy-file-rule/` with
`label_by: file_rule`. `mine` never reads or writes that folder. They are deleted by hand once enough
ID-based cases exist.

y time.

## Tool

`scripts/eval_cases.py` is standalone. It never imports or changes the review code, never runs a model,
and never writes to GitHub: `gh` is only used to read PRs, their comments and their commits.

| Folder | Holds |
| :-- | :-- |
| `scripts/` | the tool (stdlib only) |
| `cases/<repo name>/` | one JSON file per case, named by its finding ID (committed: the dataset) |
| `cases/legacy-file-rule/` | the frozen cases of the old file rule |
| `tests/` | acceptance tests with a fake `gh` |

```bash
cd agents/review-pull-request
S=eval/scripts/eval_cases.py

# Mine the dataset (needs `gh auth login`)
python3 $S mine --repo bbomt/alpha-mesh --limit 40
python3 $S mine --repo bbomt/alpha-mesh --prs 19,20          # only some PRs

# Override a case when a dismissed finding turns out to be a real bug
python3 $S label R31-e0c98f3-004 real_issue --reason "The caller never checks the length (src/x.py:40)."
```

Mining again rewrites the case files from the comments and commits but keeps any label set with
`label`. `label` also finds legacy cases (by their old `case_id`) and rewrites them in place.

## How `mine` applies the rule

For each merged PR, each review comment (`# AI Code Review` or `# Partial pull request review`) is parsed
for its `Reviewed commit` and its findings. The PR's commit messages are searched for finding IDs (GitHub
lists at most 250 commits per PR). Every line of every PR comment is matched against
`^Dismissed review finding (R<PR>-<commit7>-<NNN>): (.+)$`. Only that PR's IDs count. Any author counts,
and the first dismissal of an ID wins. Each high or medium finding with an ID is then counted in the mine
summary as `cited`, `dismissed`, `duplicate`, `conflict` or `unresolved`; a finding without an ID is
counted as `no_id`.

A finding repeated by a later review pass has a new ID, so it is its own case and must itself be cited,
dismissed or marked as a duplicate. An ID is unique only within a repo, so cases are stored per repo.

## Case file

```json
{
  "case_id": "R19-1a2b3c4-002", "repo": "bbomt/alpha-mesh", "pr_number": 19,
  "base_sha": "…", "head_sha": "<the PR's final commit>", "raised_sha": "<the commit the review covered>",
  "fix_sha": "<the commit citing the finding's ID, or null>",
  "source": "pr_comment", "model": "nvidia/nemotron-3-ultra-550b-a55b:free",
  "finding": {"file": "…", "line": 115, "severity": "high | medium", "summary": "Problem text",
              "fix": "Fix text or null", "failure_scenario": null, "id": "R19-1a2b3c4-002"},
  "label": "false_positive | real_issue",
  "label_by": "rule", "label_date": "2026-10-27",
  "label_reason": "<the dismissal's reason> | Commit 1a2b3c4 in the PR cites finding R19-…"
}
```

- The agent's severities are high/medium/low (there is no INFO).
- `case_id` and `id` are the finding's ID. Legacy cases have a hash as `case_id`, `id: null` and a
  `times_raised` count.
- `failure_scenario` is null: the agent writes a Problem and a Fix, not a separate failure scenario.
- Every case applies to `raised_sha`, the code the review saw: a real issue is fixed later, and a
  dismissed finding's file may change later. `head_sha` (the PR's final commit) is kept for reference
  only. `mesh review-pull-request` only reviews a PR's head, so scoring (PMAT-304) needs a way to review a
  given commit.
- The invalid-file-access rates are PMAT-302.
