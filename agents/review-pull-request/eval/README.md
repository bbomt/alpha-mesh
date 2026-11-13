# Evals for review-pull-request

The golden dataset (the cases, below) and the Promptfoo harness that runs the agent on it
([Running the evals](#running-the-evals)).

## What the dataset is

Three kinds of case:

- **Mined** (`cases/<repo>/`): high, medium and low findings from our own merged PRs (below). They only cover what the
  agent happened to raise.
- **Synthetic** (`cases/synthetic/`): one small commit per case, targeting every category: planted bugs the agent must
  raise, and clean changes where it must say nothing (the hallucination traps). See [Synthetic cases](#synthetic-cases).
- **Public** (`cases/public/`): merged open source PRs with a human reviewer's comment. See [Public cases](#public-cases).

Mined findings are labelled by what happened to their ID (`R<PR>-<commit7>-<NNN>`, on the finding's heading):

- **Real issue:** a commit in the PR cites the ID, e.g. `fix[PMAT-12]: fixed review finding R31-e0c98f3-003 in src/x.py`.
- **False positive:** a comment on the PR dismisses it on a line of its own:
  `Dismissed review finding R31-e0c98f3-004: <reason>`. If an ID is dismissed more than once, the last
  line wins, so a wrong dismissal is corrected by posting a new line.

Everything else is not a case: duplicates (a dismissal reason starting `duplicate of <ID>`), conflicts
(both cited and dismissed), unresolved findings (neither), findings without an ID or a line, and findings on a
commit above `--max-files`.
Running the agent against the dataset is [Running the evals](#running-the-evals).

## How to run it

From `agents/review-pull-request`. The only prerequisite is `gh auth login`. No model is called, and
nothing is written to GitHub.

```bash
S=eval/scripts/eval_cases.py
python3 $S mine --repo bbomt/alpha-mesh --limit 40            # the 40 most recently merged PRs
python3 $S mine --repo bbomt/alpha-mesh --prs 36,37           # only some PRs
python3 $S mine --repo bbomt/alpha-mesh --limit 60 --max-files 22   # how the dataset is mined
python3 $S label R31-e0c98f3-004 real_issue --reason "The caller never checks the length (src/x.py:40)."
```

`mine` prints a summary: `cited`, `dismissed`, `duplicate`, `conflict`, `unresolved`, `no_id`, `no_line`, `too_large`.

Run time grows with the files a commit changes, so the dataset is mined with `--max-files 22`: those 6 commits
hold 11 of the 14 real bugs in 75 files, and the other 7 commits add 268 files for 3 more.


## When to run it

By hand only, after PRs are merged. Nothing runs it automatically yet (PMAT-308). (This is the miner; the eval
itself runs before an agent PR merges: [The regression gate](#the-regression-gate).)

## After a run

Commit the changed files in `eval/cases/` through a ticket branch and PR.

## What a rerun does

The same PRs give the same cases. Labels set by the rules are recomputed. Labels set with `label` are
kept. Nothing is ever deleted. A case that has not changed is not rewritten. A case's `label_date` changes only
when its label or reason changes.

`expected` is recomputed from the label, unless it differs from what the rules give for the case's
current label: then it was edited by hand and is kept. `notes` and `finding` (whose line may have been
fixed by hand) are always kept. `label` recomputes
`expected` from the new label (a hand edit made for the old label is stale) and keeps `notes`.

## Synthetic cases

The agent only reads from GitHub, so the commits live in the private repo `bbomt/alpha-mesh-eval-fixtures`: branch
`agent/review-pull-request/base` holds a small orders service in Python, TypeScript and bash, and each
`agent/review-pull-request/case/<id>` is one commit on it. Its `main` branch holds the generator (`case_defs.py`,
`build.py`) and shares no history with the case branches, so a review cannot read it.

| Category | Cases | The review must | `expected` |
| :-- | --: | :-- | :-- |
| `logic` | 8 | flag it, medium or above | `raise_true_bug`, `min_severity: medium` |
| `security` | 8 | flag it as high | `raise_true_bug`, `min_severity: high` |
| `concurrency` | 4 | flag it, medium or above | `raise_true_bug`, `min_severity: medium` |
| `improvable` | 4 | suggest a change, at any severity | `raise_true_bug`, `min_severity: low` |
| `style_only` | 6 | say nothing, low included | `raise_nothing` |
| `degenerate` | 3 | not crash, not invent a review | `raise_nothing` |
| `refactor` | 4 | no high or medium finding, at most 3 of any severity | `raise_nothing`, `min_severity: medium`, `max_comments: 3` |

The generator checks that style, refactor and degenerate commits pass the base's tests, so they really do not change
behaviour, and that a planted bug's line is in a file the commit changed. Commit messages and `description` read like a
developer's and never hint at the bug. Each case has its own commit, so a true bug never overlaps a clean glob.

The model sees file paths but never the repo name (`owner` and `repo` only go into GitHub API URLs), so the name
"fixtures" does not reach it. The paths do contain `agent/review-pull-request/`.

`deg-002` (a 4,500-line lockfile) is skipped by the agent's own exclusions, so it tests that route, not how the model
copes with a large file.

## Public cases

11 cases from 10 merged PRs in well-known open source repos, picked by hand, merged 2019 to 2021, so before AI review
tools were common and the comment is a human's: 4 Python, 4 TypeScript, 3 bash. Each is `raise_true_bug` with
`min_severity: low`, at the line the reviewer commented on, on the commit they saw, with a later commit in the PR
changing the file. Humans give no severity, so any severity counts. Their `category` is `logic`; report them by folder.

The picks were checked by hand: the comment names a concrete bug, the line matches the code at that commit, and the
later commit deals with it. Human reviewers are not perfect ground truth, and reviewers miss things: an agent can find
a real bug here that no one raised. Use these for instructive comparison.

## Folder layout

| Path | Holds |
| :-- | :-- |
| `cases/<repo>/<ID>.json` | one mined case per finding ID (an ID is unique only within its repo) |
| `cases/synthetic/<id>.json` | one case per fixture commit; `pr_number` is absent and `description` is required |
| `cases/public/<id>.json` | one case per public PR review comment |
| `scripts/eval_cases.py` | the miner (stdlib only) |
| `schema/case.schema.json` | the JSON Schema every case file must match |
| `promptfooconfig.yaml`, `promptfoo/` | the harness: provider, eval adapter, loader, asserts, `case_rules.py` |
| `scripts/run_eval.sh`, `scripts/baseline.py`, `scripts/compare_baseline.py` | run, baseline, compare |
| `scripts/split_holdout.py`, `scripts/holdout_report.py` | draw the hold-out, report its totals |
| `scripts/check_gate.py`, `scripts/gate_triggers.py`, `scripts/gate_check.py`, `scripts/eval_audit.py` | the regression gate's rule, the files that need it, a PR's checklist, the audit of a master commit ([The regression gate](#the-regression-gate)) |
| `schema/review.schema.json`, `schema/baseline.schema.json` | the provider's output, the baseline's guard |
| `promptfoo/llm_rubric.py`, `judge/rubric.v1.md` | the judge and its versioned rubric ([The judge](#the-judge)) |
| `judge/sample.json`, `judge/gold-human-calibration.json`, `judge/silver-ai-calibration.json`, `judge/judge_scores.json` | the 25 calibration cases, the human (gold) scores, the silver labeller's scores, the judge's scores for them |
| `scripts/judge_sample.py`, `scripts/judge_labelling_sheet.py`, `scripts/generate_silver_calibration_labels.py`, `scripts/judge_report.py`, `scripts/judge_calibration_alignment.py` | draw the sample, write the scoring sheet, write the silver labels, report a run's scores, compute the judge's agreement with the reference |
| `baseline.json`, `error_analysis.json` | the committed baseline and the error analysis behind it (development cases only) |
| `holdout.json` | the hold-out's seed and case IDs |
| `runs/` | eval runs (gitignored) |
| `tests/` | acceptance tests with a fake `gh`, a fake GitHub and a fake model |

A case applies to `raised_sha`, the commit the review saw. `head_sha` is the PR's final commit, kept
for reference.

## What a case expects

Besides the finding and its label, every case has:

- `category`: the kind of bug: `logic`, `security`, `style_only`, `refactor`, `concurrency`, `improvable` or
  `degenerate`. `source` says where the case came from. A mined case is classed by hand, from the code at `raised_sha`,
  as the kind of bug its finding claims (a false positive too: a dismissed claim of a race is `concurrency`). `mine`
  writes a new case without one and keeps it on a rerun, so the schema test fails until the new case is classed
  (PMAT-321).
- `notes`: free text for people, saying why the case exists. The harness never reads it; the judge may.
- `expected`: what a good review of `raised_sha` does, never expected text. Every case passes or fails.
  `action` and `flag_file` are required:

| `action` | The review must | Other keys | `mine` writes it for |
| :-- | :-- | :-- | :-- |
| `raise_true_bug` | raise a finding in `flag_file` within ±3 lines of `finding.line`, at `min_severity` or above | `min_severity` (required), `must_mention_any` (optional, added by hand: the finding's text mentions at least one of these strings, ignoring case) | `real_issue` |
| `skip_false_bug` | raise no finding in `flag_file` within ±3 lines of `finding.line` (a hallucination); other findings, in that file too, are fine | none | `false_positive` |
| `raise_nothing` | raise no finding in `flag_file`, which may be a glob (hand-written clean-code cases) | `min_severity` (optional: findings below it do not count; without it, any finding counts, low included), `max_comments` (optional: fails on more findings than this at any severity) | never |

- `flag_file` is a repo-relative path. Only `raise_nothing` takes a glob, with git rules: `*` and `?` stay
  within one folder, `**` spans any depth, and the glob matches the whole path (`src/*.py` matches
  `src/a.py`, not `src/sub/a.py`; `agents/**/*.py` matches every `.py` under `agents/`).
- **A true or false bug is matched by its line.** The review runs on the frozen code of `raised_sha`, the
  commit the finding was raised on, so `finding.line` is a stable anchor: the question is whether the review
  raised a finding in the same part of the file (owner, PMAT-304). The window is `WINDOW` in
  `promptfoo/case_rules.py` (±3 lines: the widest that keeps apart the two `orchestrator/cli.py` pairs, lines
  135 / 140 and 151 / 155, where one review raised a real and a false bug in one file). So every
  `raise_true_bug` and `skip_false_bug` case must have `finding.line` (the schema enforces it).

`uv run pytest eval/tests` also checks each category's minimum count (`tests/test_dataset_coverage.py`), and checks every case file against `schema/case.schema.json` (a missing or unknown
key, a glob outside `raise_nothing`, or a true or false bug without a line, fails), and rejects a `raise_nothing` case whose `flag_file` matches
another case's file at the same repo and `raised_sha`: a file cannot be both clean and hold a finding.

## Running the evals

`promptfoo eval` runs the real agent on every case's commit and checks its findings against the case's
`expected` block (PMAT-304). `promptfoo view` shows the results.

### Models (decision record)

The owner's decisions, 2026-11-10. The model IDs are set in `.env`, not here.

1. **Agent: GLM 5.3 Flash** (`z-ai/glm-5.3-flash`, then `CODE_REVIEW_MODEL`; now `[model] review` in `review_config.toml`).
   - **Role:** runs the multi-step review graph, which fetches diffs, PR descriptions and dependency files through tools.
   - **Why:** development speed and cost.
     - The free OpenRouter tiers were unusable for evals: runs hit 8-hour timeouts and corrupted checkpoints, which
       broke the feedback loop.
     - GLM 5.3 Flash generates tokens fast, codes well and has a 1M+ context window, at a fraction of frontier
       prices, so Promptfoo iterations stay quick and within budget.
2. **Judge: MiniMax M3.1 Flash** (`minimax/minimax-m3.1-flash`, `EVAL_JUDGE_MODEL`), through OpenRouter, pinned to
   MiniMax's own host with no fallback (`EVAL_JUDGE_PROVIDER=Minimax`; owner, PMAT-311 comment 13701, 2026-11-12).
   - **Role:** grades each review on the rubric (`llm-rubric`): correctness, coverage and actionability.
   - **Why:** of five judges calibrated against the Calibration Labels, it ties for the best agreement, is the most
     consistent with itself and had no errors, at $0.0050 per judgment on its pinned host (about $0.28 per dev run). The comparison and
     the rejected models are in [CALIBRATION_AND_MODEL_DECISIONS.md](CALIBRATION_AND_MODEL_DECISIONS.md).
3. **Silver labeller: GPT-6.1 Sol** (`openai/gpt-6.1-sol`, `SILVER_CALIBRATION_MODEL`; owner, 2026-11-12).
   - **Role:** scores the frozen 25-case sample on the same rubric: the silver Calibration Labels, the stand-in for
     human (gold) scores until a human scores each case.
   - **Why:** a third family, independent of the agent and the judge, and far more consistent across its passes than
     Terra, the first labeller (correctness within ±1 100% vs 85%), for $1.58 per 3 passes.

### Baseline

> **Where the models are set:** the agent under test is `[model] review` in `review_config.toml`, committed (PMAT-312;
> unless `run_eval.sh --model` names another), and `EVAL_JUDGE_MODEL` in `.env` is the `llm-rubric` judge. Evals run on
> the free model `[model] review` sets: since PMAT-328 a full run of the 65 cases at k=3 takes about 2 hours. A paid model only
> when asked.
> Compare a run only with a baseline on the same model: `baseline.json`'s `model` says which one.

<!-- BASELINE-HEADLINE -->
`baseline.json`: run `PMAT-325-base6-nemotron`, agent `ba8705a`, `nvidia/nemotron-3.5-super-vl:free`, k=3, window ±3
lines, the 65 development cases (195 runs, 2026-11-11), all scored (no harness errors). The hold-out's score is
[reported apart](#hold-out-set).

| | Pass rate (per run) | pass^3 (per case) |
| :-- | --: | --: |
| **All 65 development cases** | **72%** | **65%** |
| `raise_true_bug` (36, recall) | 67% | 61% |
| `skip_false_bug` (20, precision traps) | 77% | 70% |
| `raise_nothing` (9, clean code) | 81% | 67% |
| Old file-only rule, for comparison | 64% | |

| Category | Expected action | Cases | Runs passed | Rate | Passed all 3 |
| :-- | :-- | --: | --: | --: | --: |
| concurrency | `raise_true_bug` | 3 | 9/9 | 100% | 3/3 |
| degenerate | `raise_nothing` | 2 | 6/6 | 100% | 2/2 |
| improvable | `raise_true_bug` | 3 | 0/9 | 0% | 0/3 |
| logic (synthetic) | `raise_true_bug` | 6 | 18/18 | 100% | 6/6 |
| logic (public) | `raise_true_bug` | 8 | 19/24 | 79% | 6/8 |
| mined: all | `raise_true_bug` | 10 | 8/30 | 27% | 1/10 |
| mined: logic | `raise_true_bug` | 10 | 8/30 | 27% | 1/10 |
| mined: all | `skip_false_bug` | 20 | 46/60 | 77% | 14/20 |
| mined: logic | `skip_false_bug` | 10 | 20/30 | 67% | 6/10 |
| mined: improvable | `skip_false_bug` | 7 | 19/21 | 90% | 6/7 |
| mined: concurrency | `skip_false_bug` | 2 | 4/6 | 67% | 1/2 |
| mined: style_only | `skip_false_bug` | 1 | 3/3 | 100% | 1/1 |
| refactor | `raise_nothing` | 3 | 5/9 | 56% | 1/3 |
| security | `raise_true_bug` | 6 | 18/18 | 100% | 6/6 |
| style_only | `raise_nothing` | 4 | 11/12 | 92% | 3/4 |

Mined cases keep their own rows (`mined:<category>` in `by_category`), like public ones. The `mined: all` rows are their
sum.

| Severity of the case's finding | Expected action | Cases | Runs passed | Rate | Passed all 3 |
| :-- | :-- | --: | --: | --: | --: |
| high | `raise_true_bug` | 9 | 21/27 | 78% | 7/9 |
| medium | `raise_true_bug` | 15 | 31/45 | 69% | 9/15 |
| low | `raise_true_bug` | 12 | 20/36 | 56% | 6/12 |
| high | `skip_false_bug` | 5 | 15/15 | 100% | 5/5 |
| medium | `skip_false_bug` | 9 | 19/27 | 70% | 6/9 |
| low | `skip_false_bug` | 6 | 12/18 | 67% | 3/6 |
| none | `raise_nothing` | 9 | 22/27 | 81% | 6/9 |

- **Failure taxonomy:** 23 `model_failure` (12 stable, 11 flaky), 0 `harness_error`, no `label_error`s fixed since the
  run (`label_errors_fixed` is empty: the 24 mined-line fixes counted in run `base2` were already in the cases it ran on).
- **The 23:** of the 6 `skip_false_bug` failures, 3 re-raise the dismissed claim and 3 are the line-matching limit only
  (a *different* claim inside the window). The 14 `raise_true_bug` failures are mostly misses (the 3 `improvable`
  cases have no finding at all); in 3 the claim is raised but below the required severity (`R45-fee8a9a-004`, `-006`)
  or at a line outside the window (`pub-trio-1832`). The 3 `raise_nothing` failures flag behaviour the commit did not
  change. Each reason is in `error_analysis.json`.
- **Recall on mined bugs is lower than 27%.** 4 of its 8 passing runs pass only because a *different* claim is inside
  the window: the agent raised the real claim at the line and severity the case requires 4 times in 30 runs (3 of them
  `R36-d2309a5-001`). The window also favours `run_eval.sh`, where three cases sit within lines 34 to 39. The judge
  (PMAT-311) is what separates these.

### Hold-out set

`holdout.json` lists 15 of the 80 cases (19%) that tuning never sees, so they can still check a change that was tuned
on the other 65, the **development cases**. Normal runs, the baseline, `compare_baseline.py` and PMAT-312's gate use the
development cases only.

**Run it only when deciding whether to ship an agent change.** Seeing its score after each experiment would make it
another tuning target.

```bash
eval/scripts/run_eval.sh --holdout --run <name>
uv run python eval/scripts/holdout_report.py eval/runs/<name>/results-holdout.json   # the totals again
```

The run writes `results-holdout.json`, apart from `results.json`, and prints totals only. **A failing hold-out case stays
unread until the set is retired**: no error analysis, no entry in `error_analysis.json`, and `baseline.py` does not ask
for one. So don't open `results-holdout.json`, the run's console log or `promptfoo view` for a run that holds these
cases, including run `base2` (it reviewed all 80).

| Hold-out, run `base2` (agent `665c767`, k=3) | Runs passed | Rate |
| :-- | --: | --: |
| **All 15 cases** (11 passed all 3 repeats) | **35/45** | **78%** |
| True bugs (10 cases) | 20/30 | 67% |
| False and clean cases (5 cases) | 15/15 | 100% |

This is a coarse check, good for catching a large drop, not a small one. Per-category rates on it are too small to read.

**How it was drawn** (`scripts/split_holdout.py`, seed 318, chosen before any failure was read; it never reads results):

- Per source and category, so that public cases, which share the category `logic` with synthetic ones, are on both
  sides. Each takes about 25% of its cases, at least 1, in whole units. Mined cases are one group per source: they
  were drawn so before they had categories, and drawing them per category would move cases between the sets
  (PMAT-321). New mined cases need the owner's decision on how they join the split.
- A unit is a PR, so cases from one PR never split; a case with no PR (synthetic) is its own unit.
- Units are taken in the order of a hash of seed and unit. One that would pass the target is skipped.
- The 8 ID-based cases of the old `base1` baseline (PRs 36 and 37) are left out: their failures were read.
- `sec-001` and `deg-001` to `-003`, whose results a smoke run showed, are drawn last.

**What this means for the set:**

- **19%, not 25%.** After PRs 36 and 37, only two mined PRs are left: PR 45 (22 cases) passes the target of 8, so only
  PR 41 (2 cases) can go. The seed has no say in that.
- **Exactly 10 true bugs**, the minimum, and 5 false or clean cases.
- **Not fully blind.** The worker that wrote the synthetic cases knows them all, and picked the public ones by reading
  their review comments. `deg-003` is in the set because every degenerate case's result was seen.

A new case changes the draw, so `test_5_...` fails on purpose: keep the list by adding the new case to development (and
changing the test to rerun the draw on the old cases), or redraw knowingly and say so on the ticket.

### The judge

A second model (`EVAL_JUDGE_MODEL`) grades each review on three dimensions, 1 to 5, each with its reason first: **correctness**
(are the findings real?), **coverage** (did it catch what matters, against the case's `expected` and `notes`?) and
**actionability** (could a developer act on it?). Coverage compares the claim itself, so a different bug near the line
counts for the line window but not for the judge.

**It sees what the agent saw** (owner, PMAT-311): the diff, the PR description and the files the agent read. It has
no tools. A claim that depends on code the agent didn't read counts as false, even if it happens to be true, because
the agent could only have guessed it; the owner scores by the same rule.

- **From the review's trace** (PMAT-325): each path's first `read_file` result, exactly as the agent got it, with no
  file read again. That includes a not-found result's hints. A repeat's "already read" note is skipped, and an
  excluded path is dropped. A trace that is gone, or that lacks a reviewed file, gets a judge error, not a score.
- **A review without a trace** (a run before PMAT-325) fetches the files in `files_read` (PMAT-322) again at the same
  commit with the agent's `max_file_lines`. This stays until every run has a trace, and then PMAT-334 makes a missing
  trace an error (owner, PMAT-325 comment 13728). A review with no `files_read`, or a file that can't be fetched again,
  gets a judge error.
- **Both give the same files.** On base6, every file whose content the agent got matched the refetch. The only
  difference is that a refetch of a missing path lacks the agent's hints. So the calibration below holds for both
  (owner, PMAT-325 comments 13724 and 13728).

Either way it is one set of files for the whole review, so the judge can't tell which file's review read what. The
folder listing, the agent's overview and its instructions are left out.

**It never gates.** `judge_results` always passes, so a case's pass or fail, `baseline.json` and the comparison don't
change. A judge error is recorded, not failed. A review with no findings has `null` correctness and actionability.

It is an LLM judge in a python assert, not Promptfoo's built-in `llm-rubric` (owner, PMAT-311), because everything sent to
a model has to go through `ReviewSession`, so an excluded file's content never reaches it
(`tests/test_canary.py`), and one call has to return all three scores. It refuses hold-out cases.
`promptfooconfig.yaml` shows how it fits with the other asserts.

Set `EVAL_JUDGE_MODEL`, `EVAL_JUDGE_URL` and `EVAL_JUDGE_TOKEN` in `.env`. A missing one stops the run before any review.

```bash
eval/scripts/run_eval.sh --stored ~/.mesh/eval-runs/PMAT-322-base3 --run judge1   # judge stored reviews; the agent doesn't run
uv run python eval/scripts/judge_report.py eval/runs/judge1/results.json           # means, proposal, cost
JUDGE_LIVE=1 uv run pytest eval/tests/test_judge.py -k live                        # the checks that need the real model
```

A normal `run_eval.sh` run judges the agent's fresh reviews too. A `--stored` run has no `run.json`, so it can't become the baseline.

**Which repeats the judge scores** (`--judge-repeats`), like review passes (incremental while iterating, full before
DONE): `first`, the default, judges repeat 0 of each case only, for iterating on the agent; `all` judges every repeat,
before merging an agent change. The line checks run on every repeat either way. Repeats cost money only through the
judge, and they mostly add precision to the mean: a change's direction shows on repeat 0 across all cases, while the
merge decision wants the precise mean. The report counts the skipped repeats (`N not judged (repeat 0 only)`).

**Judge Calibration (meta-evaluation):** humans score 25 development cases blind (gold), a silver labeller scores them
too (below); together these are the **Calibration Labels**. The judge's scores are compared with them, gold where set, else
silver, and the result is the **Judge Alignment** (within ±1 point per
dimension, and Cohen's kappa on pass/fail, score >= 4).

```bash
uv run python eval/scripts/judge_labelling_sheet.py ~/.mesh/eval-runs/PMAT-322-base3   # sheet and labels file; no scores shown
uv run python eval/scripts/judge_report.py eval/runs/judge1/results.json --write-scores  # after the labels are committed
uv run python eval/scripts/judge_calibration_alignment.py --silver-calibration eval/judge/silver-ai-calibration.json \
    --gold-calibration eval/judge/gold-human-calibration.json eval/judge/judge_scores.json
```

**Silver labels (PMAT-326):** the owner has no time to score the 25 cases by hand yet, so a model of another family scores
them on the same rubric: the **silver standard**, a shortcut to the first calibration values. Humans remain the **gold
standard**. The cases are about code (is the finding real, was the bug caught, can a developer act on it), not tone, so
a strong model can stand in until humans score.

```bash
uv run python eval/scripts/generate_silver_calibration_labels.py ~/.mesh/eval-runs/<run>   # SILVER_CALIBRATION_MODEL in .env
uv run python eval/scripts/generate_silver_calibration_labels.py ~/.mesh/eval-runs/<run> --model openai/gpt-5-nano   # dev: cheap
```

The labeller is `SILVER_CALIBRATION_MODEL` in `.env` (`openai/gpt-6.1-sol`, owner decision 2026-11-12), or `--model`.

- It gets what the judge and the sheet get (rubric, ground truth, findings, PR description, diff, files the agent read),
  through `llm_rubric.ask` and so `ReviewSession` (`tests/test_canary.py`). It reads only repeat 0's stored review: never
  a judge score, a line-window result or a hold-out case. This sends the sample's repo code to the model's vendor via
  OpenRouter (owner's choice, 2026-11-10), at `SILVER_CALIBRATION_URL` with `SILVER_CALIBRATION_TOKEN`. These are its
  own, not the judge's, so the judge can be on another host. A missing one stops the run
  before anything is sent.
- Each reply is strict JSON (`structured_outputs`, `why` before `score` per dimension, `reasoning_effort` high by
  default). A missing or out-of-range score stops the run naming the case and pass; it is never defaulted.
- 3 passes (odd), the median per dimension. A case whose passes differ by more than 1 point is `unstable` and worth a
  human check first. The run prints the cost and the passes' self-agreement per dimension.
- `judge/silver-ai-calibration.json` (committed) holds the scores only; its header names the model requested and the
  model and providers reported, the effort, rubric, repeat, passes, cost, date and self-agreement, so a rerun with
  another model keeps the path. The reasons are in `runs/judge-labelling/silver-ai-calibration.md` (gitignored: it
  quotes reviews and code).
- **The committed labels are `openai/gpt-6.1-sol`** (owner, 2026-11-12): 3 passes, effort high, $1.58. Passes agree
  exactly / within ±1 on correctness 85% / 100%, coverage 96% / 96%, actionability 90% / 100%; 1 of 25 cases unstable.
  They replaced `openai/gpt-6.1-terra` (2026-11-10: correctness 60% / 85%, 6 unstable, $0.90; in git history).
- **Cost:** one run sends about 1.02M input tokens (339k per pass, measured); passes 2 and 3 repeat pass 1's prompts, so
  OpenRouter mostly bills them at the cached-input price (5% for these models). Output, mostly reasoning, varies by
  model: Terra about 90k–105k over 3 passes (inferred from its cost), nano about 600k. **A higher model run**
  (`openai/gpt-6-astra` about $9–21 for 3 passes) **needs the Team Lead's sign-off**: develop and test with a cheap model.

**Gold over silver:** `judge_calibration_alignment.py` builds the reference per case and dimension when it runs, never in a file: the
human score where one is set, else silver's. A `TODO` is ignored, so with no human scores the reference is all silver,
and adding human scores later needs no silver rerun. It prints how many reference scores per dimension came from each
source (`correctness: 1 gold, 24 silver`) and lists every silver-vs-gold difference over 1 point: where silver was wrong.
**Humans score blind:** from `sheet.md` into `gold-human-calibration.json`, never after reading the silver scores or
reasons. **Caveat:** the silver labeller and the judge read the same rubric, so an ambiguity both read the same way is
invisible to this calibration. That is what gold is for.

The sample (`scripts/judge_sample.py`, seed 311) was drawn from the case files only, per group, from the development
cases. **It is frozen:** your labels are on these 25 cases, so it is never redrawn, even when categories change (PMAT-321)
or cases are added; the script refuses, and a test pins the list. A new sample means new labels. Under about 75% on a dimension: read the disagreements, tighten the rubric in a new version (`rubric.v2.md`), and
record the iteration here. Changing `EVAL_JUDGE_MODEL` or the rubric means aligning again.

<!-- JUDGE-RESULTS -->
**Judge Alignment** (2026-11-12): judge `minimax/minimax-m3.1-flash` on OpenRouter, host Minimax only (every call
served there), rubric v1, base3 repeat 0, against the silver Calibration Labels (`openai/gpt-6.1-sol`; 0 gold):

| Dimension | One pass (`judge/judge_scores.json`) within ±1 / kappa | Median of 3 passes | Self ±1 across passes | n |
|---|---|---|---|---|
| correctness | 85% / 0.74 | 90% / 0.86 | 95% | 20 |
| coverage | 100% / 1.00 | 100% / 1.00 | 100% | 25 |
| actionability | 100% / undefined | 100% / undefined | 100% | 20 |

Every dimension is over the ~75% bar, so rubric v1 stands. Actionability's kappa is undefined: almost every review
passes for both raters, so read its ±1 figure. `judge_scores.json` is pass 2 of 3, the first with no judge error
(pass 1 had one empty reply). Cost: $0.0050 per judgment ($0.31 for the 3 passes). How the labeller and judge were
chosen, and the earlier results: [CALIBRATION_AND_MODEL_DECISIONS.md](CALIBRATION_AND_MODEL_DECISIONS.md).

**The current agent against the proposal (provisional)** (2026-11-12): base6 (`PMAT-325-base6-nemotron`, agent
`nvidia/nemotron-3.5-super-vl:free`, k=3, all 3 repeats judged), the judge above. 195 rows: 163 model calls, 22 fixed by
the rubric, 10 judge errors (6 files GitHub didn't return during a load spike, 2 replies missing a score, 1 timeout),
$0.93.

| Measure | Agent | Starting proposal |
|---|---|---|
| Mean correctness | 2.97 | ≥ 4.0 (fails) |
| Mean coverage | 3.80 | ≥ 3.8 (meets, just) |
| Mean actionability | 4.89 | none |
| Security misses (coverage ≤ 2) | 0 | 0 (meets) |
| `style_only` pass rate | 93% | 100% (fails) |

Correctness is lowest on `refactor` (1.50), `style_only` (1.75) and `improvable` (2.29): findings that aren't worth fixing
or aren't real (`judge_report.py` prints every group). These numbers are provisional: the owner sets the final
thresholds after agent tuning, and PMAT-312 starts as a no-regression gate.

**Pinning:** `EVAL_JUDGE_PROVIDER` (OpenRouter only) sends every judge call to one host, with no fallback, so every
judgment comes from the same weights. A down host gives judge errors, which the report counts; it never silently
switches host. Unset, OpenRouter spreads calls across hosts at different quantisations. A pin with a non-OpenRouter
`EVAL_JUDGE_URL` stops the run before any review.
- **What is recorded:** each judgment records the requested and reported model, the host, its cost, tokens, and cached
  and reasoning tokens. The report sums them and lists the hosts that served the run.
- **Calls the rubric makes unnecessary:** a `raise_nothing` case whose review has no findings scores coverage 5 with
  null correctness and actionability (the rubric fixes every score), so it isn't sent to the model.
- **Prompt order:** the shared context (the PR description, diff and files read) comes before the findings and the
  case, so the cases of one review share a cached prefix.

A security miss in `judge_report.py` is a security case with coverage 2 or below.

**Family:** each role uses a different model family, so no model grades its own family's output.

| Role | Model | `.env` variable |
|---|---|---|
| Agent | GLM (`z-ai/glm-5.3-flash`) | `[model] review` in `review_config.toml` |
| Judge | MiniMax (`minimax/minimax-m3.1-flash`) | `EVAL_JUDGE_MODEL` |
| Silver labeller | OpenAI (`openai/gpt-6.1-sol`) | `SILVER_CALIBRATION_MODEL` |

The warning at the start of a run checks the run's model and `[model] review` against the judge's family. A run
reviewed by a MiniMax model would be graded by its own family. The judge would then have to move family and be aligned
again. The warning doesn't check the silver labeller, so keep it out of the agent's family too.

### Terms

| Term | What it is | Changes when |
| :-- | :-- | :-- |
| **The agent** | The review agent, the thing being evaluated | With its code |
| **Running the agent** | `run_review_agent`: the slow step that produces a review | |
| **Stored review** | A review saved from an earlier run | Never |
| **The judge** | The separate model that scores a review | With `EVAL_JUDGE_MODEL` or the rubric |
| **Calibration Labels** | Trusted scores on the 25 frozen sample reviews, the yardstick for the judge: **gold** (a human's) where set, else **silver** (a model of another family) | Gold when a human scores; silver when the labeller reruns |
| **Judge Calibration** | Running the judge on the sample reviews and comparing its scores with the Calibration Labels | With `EVAL_JUDGE_MODEL` or the rubric |
| **Judge Alignment** | The result of Judge Calibration: how often the judge agrees with the Calibration Labels (within ±1 point, and kappa) per dimension | Each Judge Calibration |
| **Golden dataset** (labelled dataset, eval set) | The cases: input + **ground truth** (`real_issue` / `false_positive`, and the `expected` block), split into **development cases** and a **hold-out** | A **label error** is found and fixed |
| **Ground truth** | The correct answer for a case, decided independently of the agent: the label (a fix commit citing the finding ID, or a dismissal) and the `expected` block derived from it | Only through a label-error fix |
| **Run** | One `promptfoo eval`: the agent's output and pass / fail / error for every case, k times | Every run |
| **Error analysis** | Reading every failing case of a run and classifying **why** it failed. It describes the result and changes nothing in the agent | Once per new failure |
| **Failure taxonomy** | The classes failures are sorted into: `model_failure` / `label_error` / `harness_error`, with counts | Each error analysis |
| **Baseline** | The reference result for one agent version: per case pass/fail per run, pass^k, failure class (`baseline.json`) | Only on purpose, after an intended agent change |
| **pass^k** | Share of cases that pass all k runs (consistency; τ-bench). A case failing all k runs is a **stable** failure; failing some is **flaky** | Every baseline |
| **Precision / recall** | `skip_false_bug` and `raise_nothing` cases (hallucination traps) measure precision and the false-positive rate; `raise_true_bug` cases measure recall | Every run |
| **Test–production parity** | The eval runs the same review code as `mesh review-pull-request`; only the adapter that supplies `(repo, base, head, description)` differs | Never, by design |

### How a case is reviewed

A PR is a repo plus a base and a head commit; what it shows is the three-dot diff `base...head`. A case's
`raised_sha` is the head the agent saw when it raised the finding, so a case is reviewed as the diff
`base_sha...raised_sha`, with files read at `raised_sha` and the PR's description.

`mesh review-pull-request` (the PR adapter) and the **eval adapter** (`promptfoo/eval_adapter.py`) call the
same function, `run_review_agent(owner, repo, base, head, description)`. The eval adapter posts nothing. It
reads the findings (file, line, severity, summary) from the review's final graph state, the data the PR
comment is rendered from, so nothing parses markdown. The provider (`promptfoo/agent_provider.py`) returns
them as JSON (`schema/review.schema.json`; extra fields are allowed, for later asserts).

`trace` is the path of the review's **trace** (PMAT-325), `runs/<run>/traces/repeat-<n>/<commit>.jsonl`: every
tool call (name and arguments) and its result exactly as the model received them, in order, recorded inside
the redactor (`src/review_pull_request/trace.py`). That includes not-found results with their hints,
"already read" notes and refusals, so the judge can rebuild the agent's context without reading a file
again. The records are shaped like Langfuse observations (a `trace`, a `span` per file review, a `tool` per
call). A file review's records are written when it ends, so a stopped review leaves no half span; a resume
appends to the same file, and `trace.load()` keeps the last span of a file reviewed twice. The trace never
goes into the checkpoint store or the results JSON (only its path does). It is `null` when every changed
file was excluded.

`files_read` lists the repo-relative paths whose content the agent's model was shown, across the whole review,
taken from the trace. It holds paths only, never content, and a not-found, refused or failed read doesn't
count. A read stops at `max_file_lines`, so the path alone doesn't say how much of a file was shown. If a
file of the review has no span in the trace (reviewed before the trace existed), `files_read` is left out,
because `[]` would say the agent read nothing. New checkpoints no longer hold `files_read`.

- **One review per commit and repeat.** Several cases share a commit. Within one repeat they share one review
  (they queue on a lock per commit and repeat, then reuse the first's review); each repeat is a fresh review,
  so pass^k measures something, and the repeats of a commit review at the same time (PMAT-328). Promptfoo
  runs 8 cases at once (`maxConcurrency` in `promptfooconfig.yaml`; `--max-concurrency <n>` overrides it).
- **Checkpoints** go to `runs/<run>/checkpoints-repeat-<n>.sqlite` (LangGraph's SqliteSaver), never to the
  production DynamoDB store, so a run never touches the checkpoint of a real PR review. Rerunning the same
  `--run` reuses finished reviews and resumes a stopped one from its last finished file (free models throttle).
- **Errors are not failures.** A review that leaves no result, or a case whose `flag_file` was not fully
  reviewed (unusable model response), is a Promptfoo error: `harness_error`, left out of the pass rate.

### How to run

From `agents/review-pull-request`. Prerequisites: `gh auth login`, the repo's `.env` (model URL and token),
`uv sync`, Promptfoo (`pnpm add -g promptfoo`) and Node 24 under nvm (the script switches to it for its own
session; the machine default stays). No AWS is needed.

```bash
eval/scripts/run_eval.sh --run base1                                                  # k=3, every development case (.env model)
eval/scripts/run_eval.sh --run base1                                                  # again: resumes
eval/scripts/run_eval.sh --k 1 --run smoke --filter-pattern '^R37-1fbb920-001$'       # one case (.env model)
eval/scripts/run_eval.sh --run base1 --max-concurrency 4                              # fewer workers (default 8)
uv run python eval/scripts/compare_baseline.py eval/runs/base1/results.json           # vs baseline.json
uv run python eval/scripts/baseline.py eval/runs/base1                                # write a new baseline
PROMPTFOO_CONFIG_DIR=eval/runs/base1/.promptfoo promptfoo view                        # browse (Node 24)
```

`run_eval.sh` records the run's commit, model, date, k, window and `dirty` (uncommitted changes in `src/`, `eval/promptfoo/` or `eval/cases/`: such a run never becomes the baseline) in `runs/<run>/run.json`. One run folder
holds one model's reviews (the checkpoint key has no model), so a new model needs a new `--run`. A resumed run
keeps the commit and date it started at (so label fixes made between passes are counted), and is refused if
the agent changed since, or with another model or k (`scripts/run_meta.py`).

### The regression gate

An agent PR merges only if its eval keeps the baseline's score, within the noise (PMAT-312). It runs on a dev box,
not in GitHub Actions: no Actions minutes, no secrets in GitHub.

**The steps for an agent PR.** The full gate is expensive (about a day's free quota), so it runs once, at merge time,
after the team lead has accepted the rest of the ticket's code:

```bash
mesh eval-gate --check https://github.com/bbomt/alpha-mesh/pull/<n>   # anyone: the checklist and the next command
mesh merge-pr  https://github.com/bbomt/alpha-mesh/pull/<n>           # the team lead: if the gate is all that is missing,
                                                                     #   it offers to run it now, then merges [y/N]
mesh eval-gate https://github.com/bbomt/alpha-mesh/pull/<n>           # the gate on its own: about 75 min; again: resumes
```

Without a terminal (a worker), `mesh merge-pr` refuses instead and prints the same checklist:

```
[x] PR open (open)
[x] up to date with master
[x] master audit: success
[x] changes gated files: agents/review-pull-request/src/review_pull_request/prompts/per_file.md
[ ] evals on head 1a2b3c4: missing
next: mesh eval-gate https://github.com/bbomt/alpha-mesh/pull/<n>
```

Also:

```bash
mesh eval-gate --override <PR url> --reason "<why>"   # evals = success by hand ("override: <why>"), recorded on the PR
mesh eval-gate --master                               # the gate on master's head: clears a red master, or opens the revert PR
uv run python eval/scripts/check_gate.py eval/runs/<run>        # the gate rule on any finished run, by hand
```

- **What needs it:** a PR that changes a file in `scripts/gate_triggers.py`: `src/**` (code and the prompts in
  `src/review_pull_request/prompts/`), `review_config.toml`, `pyproject.toml`, `uv.lock`, `promptfoo/**`,
  `promptfooconfig.yaml`, `cases/**`, `holdout.json`. Docs, tests, `scripts/`, `judge/` (report only) and
  `baseline.json` (a rebaseline PR, approved by the owner) do not.
- **The model it runs on** is `[model] review` in `review_config.toml`, the model the reviews themselves use: the gate
  measures the agent as it runs (committed: a model change is a PR, so the gate sees it). `baseline.json` must be on that model (a test checks it), or every gate run fails as a model change.
- **What `mesh eval-gate` does:** in a clean worktree at the PR's head (refused if the branch is behind master), it
  runs the development cases at k=3 with every repeat judged (`--judge-repeats all`), then `check_gate.py` against
  **master's** `baseline.json` and master's `TOLERANCE`, so a PR cannot loosen its own gate. It posts the verdict as
  the commit status `evals` on that head (`pending` while it runs) and the score table as a PR comment. A new push is
  a new head: `mesh merge-pr` still accepts the older head's success when no gated file changed since (a README or
  test fix after review), and otherwise needs a new gate run.
- **The rule (`check_gate.py`):** it counts the runs (case × repeat) that pass their case's line check, on the cases
  the baseline also holds, and fails when the count drops more than `TOLERANCE` (10) below the baseline's. It counts
  runs, not cases: in the A/A test (the same code as base6, run again as `PMAT-312-aa1`), 14 of the 65 cases changed
  result (10 new failures, 4 new passes) while the runs passed moved from 140/195 to 142/195. The rows per category
  move too (`public` went from 19/24 to 14/24), so only the total gates. 10 is provisional, from that one sample:
  tighten it as same-code runs come in, or as the agent gets less flaky.
- **It also fails, whatever the count,** on a harness error (a run stopped by the model's quota never passes), a
  missing case or repeat, or a run on another model or k than the baseline's: a model change needs a new baseline
  first.
- **Reported, not gating yet** (the owner switches them on): security misses, failing `style_only` runs, and the
  judge's means against PMAT-311's provisional proposal.
- **Cost:** the agent is free; the judge is about $0.005 per judged run, so about $1 per gate run. **Time:** about
  75 min (`PMAT-312-aa1`: 1 h 16 min at concurrency 8). **Quota:** a full run uses about a day's free-model quota
  on OpenRouter (`free-models-per-day-high-balance`; the second A/A run on 2026-11-13 hit it after 32 minutes), shared
  with every `mesh review-pull-request`. Run `mesh eval-gate` again after the reset: it resumes.
- **A merge that skips the gate is caught, not prevented.** The repo is private on GitHub's free plan, so `evals`
  cannot be a required check and GitHub's merge button ignores it. So on every push to master the `eval-audit`
  workflow (`scripts/eval_audit.py`, no model call) posts `eval-audit` on the commit: green when it came from a PR
  whose gate passed on a head holding master's previous commit; red when it changes a gated file without that (a
  manual merge, a failing or stale gate, a push without a PR), with a comment on the PR and a failed run that GitHub
  emails about; otherwise its parent's colour. **While master is red, `mesh merge-pr` merges nothing** but the
  revert PR (branch `eval-revert/...`) or a gated PR whose gate passed. `mesh eval-gate --master` runs the gate on
  master: a pass turns it green; a failure opens the revert PR of the ungated commit.
- **Known limit:** anyone with write access can post a status by hand (that is what `--override` does, visibly).
  The gate guards against mistakes, not against someone bypassing it on purpose.

#### Reading a gate report

`mesh eval-gate` posts it as a PR comment and prints it; `check_gate.py eval/runs/<run>` prints it for any finished
run, and `promptfoo view` (see [How to run](#how-to-run)) shows each review.

```
runs passed 142/195 (baseline 140), drop -2, tolerance 10
| Category | Expected action | Cases | Baseline runs passed | Run runs passed |
| security | `raise_true_bug` | 6 | 18/18 | 18/18 |
| public   | `raise_true_bug` | 8 | 19/24 | 14/24 |
```

- **The first line is the verdict.** 195 runs = 65 development cases × 3 repeats; each run passes when the review
  does what its case expects (raise the true bug, skip the false one, say nothing on clean code). The baseline passed
  140 (72%): that is the agent **today**, not a target. The gate asks only "no worse than today, beyond the noise".
- **The rows say where a drop comes from.** They are noisy on their own (a row of 24 runs moved by 5 between two runs
  of the same code), so read a row's change as a lead for error analysis, not a verdict.
- **"Reported, not gating yet"** is how good the agent is in absolute terms (PMAT-311's provisional proposal, for
  example judge correctness >= 4.0 where the agent scores about 2.8). They become gates when the owner switches them on.

#### The tolerance T

`TOLERANCE` in `scripts/check_gate.py`, committed: the drop in runs passed the gate allows. Smaller is stricter; 0
fails any drop at all, which the noise alone would trip often. `mesh eval-gate` always uses master's value, so a PR
cannot loosen its own gate, and there is no flag for it: retrying a failed gate with a larger T until it passes would
make the gate meaningless. Change T in its own reviewed PR, from same-code runs (A/A). To see a finished run under
another T, without running anything again: `check_gate.py eval/runs/<run> --tolerance 5`.

#### Rebaselining, and new cases

Rebaselines are made by hand, never automatically, in the PR whose change is meant to move the score (a prompt
rework, the temperature, a new model):

1. The gate runs on the PR as usual. A **model** change always fails it (the run's model is not the baseline's): the
   owner reads the new run's report and uses `mesh eval-gate --override <PR url> --reason "new model: rebaselined"`.
2. `uv run python eval/scripts/baseline.py eval/runs/gate-pr<n>-<sha7>` writes `baseline.json` from that gate run. It
   first asks for an entry in `error_analysis.json` for every failing case ([Error analysis](#error-analysis)).
3. Commit `baseline.json` and `error_analysis.json` in the same PR. Neither is a gated file, so the passing status
   carries over; the owner approves the new baseline in review.

**New cases** (mined from merged PRs with `scripts/eval_cases.py mine`, PMAT-308) change `cases/**`, a gated file,
so their PR runs the gate. A case the baseline does not hold yet is run and reported as "new since the baseline, not
gated"; it joins the gate at the next rebaseline. A new mined case also needs the owner's decision on how it joins
the hold-out split ([Hold-out set](#hold-out-set)).

#### When master is red

`eval-audit` failed on master: an agent change landed without a passing gate. GitHub emails the owner (a failed
workflow run), and the PR gets a comment. Until it is fixed, `mesh merge-pr` merges nothing else.

1. The team lead runs `mesh eval-gate --master`. If the gate passes on master, master turns green and that is all.
2. If it fails, it opens the revert PR (branch `eval-revert/<sha7>`) of the ungated commit and prints, and writes in
   the PR, what happened and what to run next: `mesh eval-gate <revert PR>`, then `mesh merge-pr <revert PR>`. Nothing
   is merged automatically.
3. The developer of the reverted change redoes it on a new branch, and its PR goes through the gate.

### Error analysis

After a run, read every failing development case (`compare_baseline.py` lists the new ones; `baseline.py` refuses to
write until each failing case is classed) and record why it failed in `error_analysis.json`:

```json
{"R36-812f904-001": {"failure_class": "model_failure", "reason": "Re-raises the dismissed save_queue ordering claim at cli.py:148."}}
```

- `model_failure`: the agent got it wrong (re-raised the false finding, missed the real one). Leave it
  failing: this is what the eval measures. **When in doubt, it stays a `model_failure`**; a disputed case
  stays failing and is listed for the owner to decide.
- `label_error`: the ground truth is wrong, not the agent. Fix the case: a wrong label with
  `eval_cases.py label <ID> real_issue --reason "..."`, or a wrong `expected` or `finding.line` by hand,
  with the reason in `notes`. A mined line is the one the review cited, which can be off: the case is
  matched at the line where the described code is at `raised_sha`. A line fix moves it to exactly that line, even from
  within ±3 (owner, PMAT-318), whichever way it moves the score.
- A correct finding that isn't worth fixing counts as false, unless it has a concrete trigger reachable in this repo
  (owner, PMAT-318). So re-raising one stays a `model_failure`.
- `harness_error`: the pipeline failed (crash, no result, findings misread). Classed automatically; open a
  ticket. Left out of the pass rate and listed separately.
- Never quietly delete a failing case.

**Guard against labelling to the test.** A label fix always turns a failure into a pass, so every fix:

1. cites evidence in its reason: file:line and why the original decision was wrong ("agent was right" is
   not a reason);
2. is its own commit, `fix[PMAT-XX]: fixed label error in eval case <ID> (<short reason>)`
   (`baseline.py` refuses uncommitted changes in `cases/`);
3. is counted: `baseline.py` reads every label change since the run's commit from git into
   `label_errors_fixed` (case ID and reason), next to the pass rate, and `compare_baseline.py` lists every
   label change since the baseline. `schema/baseline.schema.json` fails a baseline whose label fix has no
   case ID or reason, or whose failing case has no class or reason (tested in `tests/test_baseline.py`).

`baseline.py` re-scores the stored reviews against the case files as they are now, so a fixed label counts
without reviewing again. It also records each case's result under the old file-only rule
(`file_level_runs`, `file_level_pass_rate`), to show what line matching changed.

### Security

A case file is input: mined cases hold text from PR comments, and a case's values reach git, `gh` and GitHub API
paths that carry the token. So the loader refuses a case whose `case_id`, `repo` or commit SHAs are not plain IDs
(`eval/schema/case.schema.json`), and Promptfoo's templating is off (`run_eval.sh`), since it renders string vars as
Nunjucks, which can run code. Run the evals with `run_eval.sh`, not `promptfoo` directly. Audited for PMAT-304; the
findings are on the ticket.

### Known limitations

- **Selection bias.** Mined cases were selected by the model that raised them, so they favour or penalise
  that model. Compare agent versions on the full set; compare models on the synthetic and public cases.
  Each case's `model` field records its source; no per-model split is reported.
- **Line matching is a proxy:** the same claim cited outside the window (at a call site, say) is missed, and a different
  bug inside the window counts as the same one. The judge's coverage score compares the claim (`judge_report.py`).
- **The PR description is today's**, fetched at run time; it may have been edited since `raised_sha`.
- **Mined cases leave out large commits** (`--max-files 22`): 3 real and 19 false findings on 7 commits.
- **Long reviews can time out in Promptfoo.** The time a case waits on its lock counts towards the provider's
  2-hour timeout: a case queued behind a large review of the same commit in the same repeat (19–20 files:
  75–110 minutes) can pass it and become a `harness_error` (PMAT-330). Rerunning the same `--run` finishes it
  from the checkpoints.
- **The free models' daily quota** (OpenRouter `429 free-models-per-day`) does not cover a k=3 baseline in
  one day: rerun the same `--run` after it resets (00:00 UTC).
