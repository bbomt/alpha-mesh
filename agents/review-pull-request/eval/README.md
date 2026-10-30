# Eval cases for review-pull-request

## What the dataset is

High and medium review findings from merged PRs, each labelled by what happened to its ID
(`R<PR>-<commit7>-<NNN>`, on the finding's heading):

- **Real issue:** a commit in the PR cites the ID, e.g. `fix[PMAT-12]: fixed review finding R31-e0c98f3-003 in src/x.py`.
- **False positive:** a comment on the PR dismisses it on a line of its own:
  `Dismissed review finding R31-e0c98f3-004: <reason>`. If an ID is dismissed more than once, the last
  line wins, so a wrong dismissal is corrected by posting a new line.

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
kept. Nothing is ever deleted. A case that has not changed is not rewritten. A case's `label_date` changes only
when its label or reason changes.

`expected` is recomputed from the label, unless it differs from what the rules give for the case's
current label: then it was edited by hand and is kept. `notes` is always kept. `label` recomputes
`expected` from the new label (a hand edit made for the old label is stale) and keeps `notes`.

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
| `schema/case.schema.json` | the JSON Schema every case file must match |
| `tests/` | acceptance tests with a fake `gh` |

A case applies to `raised_sha`, the commit the review saw. `head_sha` is the PR's final commit, kept
for reference.

## What a case expects

Besides the finding and its label, every case has:

- `category`: `mined` for mined cases. Hand-written cases (later) use `logic`, `security`, `style_only`,
  `refactor`, `concurrency`, `improvable` or `degenerate`.
- `notes`: free text for people, saying why the case exists. The harness never reads it; the judge may.
- `expected`: properties a good review of `raised_sha` has, never expected text. `mine` derives it from
  the label:

| Label | `expected` |
| :-- | :-- |
| `real_issue` | `{"must_flag": true, "flag_file": <file>, "min_severity": <severity>}` |
| `false_positive` | `{"must_not_reraise": true, "flag_file": <file>}` |

| Key | Means |
| :-- | :-- |
| `must_flag` | `true`: the review must raise a finding in `flag_file`. `false` (hand-written traps): it must raise none there. |
| `must_not_reraise` | the review must not raise this finding again; it may raise others. The check only looks at `flag_file`, so it is a weak signal: the judge decides whether a finding there is the same one. |
| `flag_file` | the file the finding is in |
| `min_severity` | the lowest severity (`high`, `medium`, `low`) that counts as flagging it |
| `must_mention_any` | optional, added by hand: the finding must mention at least one of these strings |
| `max_comments` | optional, hand-written cases: the most findings the review may raise |

`uv run pytest eval/tests` checks every case file against `schema/case.schema.json`, so a case without
`expected` (or with an unknown key in it) fails the tests.
