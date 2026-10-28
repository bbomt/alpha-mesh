# Eval cases for review-pull-request

## What the dataset is

High and medium review findings from merged PRs, each labelled by what happened to its ID
(`R<PR>-<commit7>-<NNN>`, on the finding's heading):

- **Real issue:** a commit in the PR cites the ID, e.g. `fix[PMAT-12]: fixed review finding R31-e0c98f3-003 in src/x.py`.
- **False positive:** a comment on the PR dismisses it on a line of its own:
  `Dismissed review finding R31-e0c98f3-004: <reason>`.

Everything else is not a case: duplicates (a dismissal reason starting `duplicate of <ID>`), conflicts
(both cited and dismissed), unresolved findings (neither), findings without an ID, and low findings.
Running the agent against the dataset (scoring) is PMAT-304.

## How to run it

From `agents/review-pull-request`. The only prerequisite is `gh auth login`. No model is called, and
nothing is written to GitHub.

```bash
S=eval/scripts/eval_cases.py
python3 $S mine --repo bbomt/alpha-mesh --limit 40            # the 40 most recently merged PRs
python3 $S mine --repo bbomt/alpha-mesh --prs 36,37           # only some PRs
python3 $S label R31-e0c98f3-004 real_issue --reason "The caller never checks the length (src/x.py:40)."
```

`mine` prints a summary: `cited`, `dismissed`, `duplicate`, `conflict`, `unresolved`, `no_id`.

## When to run it

By hand only, after PRs are merged. Nothing runs it automatically yet (PMAT-308).

## After a run

Commit the changed files in `eval/cases/` through a ticket branch and PR.

## What a rerun does

The same PRs give the same cases. Labels set by the rules are recomputed. Labels set with `label` are
kept. Nothing is ever deleted.

## The legacy cases

The 49 cases in `cases/legacy-file-rule/` came from the old file rule (PMAT-300): a finding whose file
did not change before the merge. `mine` never reads, rewrites or recreates them (their findings have no
ID). `label` can still relabel them. They are deleted by hand once enough ID-based cases exist.

## Folder layout

| Path | Holds |
| :-- | :-- |
| `cases/<repo>/<ID>.json` | one case per finding ID (an ID is unique only within its repo) |
| `cases/legacy-file-rule/` | the frozen legacy cases |
| `scripts/eval_cases.py` | the tool (stdlib only) |
| `tests/` | acceptance tests with a fake `gh` |

A case applies to `raised_sha`, the commit the review saw. `head_sha` is the PR's final commit, kept
for reference.
