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

By hand only, after PRs are merged. Nothing runs it automatically yet (PMAT-308).

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
| `schema/review.schema.json`, `schema/baseline.schema.json` | the provider's output, the baseline's guard |
| `promptfoo/llm_rubric.py`, `judge/rubric.v1.md` | the judge and its versioned rubric ([The judge](#the-judge)) |
| `judge/sample.json`, `judge/human_labels.json`, `judge/judge_scores.json` | the 25 hand-scored cases, the owner's scores, the judge's scores for them |
| `scripts/judge_sample.py`, `scripts/judge_labelling_sheet.py`, `scripts/judge_report.py`, `scripts/judge_agreement.py` | draw the sample, write the scoring sheet, report a run's scores, compute judge–human agreement |
| `baseline.json`, `error_analysis.json` | the committed baseline and the error analysis behind it (development cases only) |
| `holdout.json` | the hold-out's seed and case IDs |
| `runs/` | eval runs (gitignored) |
| `tests/` | acceptance tests with a fake `gh`, a fake GitHub and a fake model |

A case applies to `raised_sha`, the commit the review saw. `head_sha` is the PR's final commit, kept
for reference.

## What a case expects

Besides the finding and its label, every case has:

- `category`: `mined` for mined cases. Hand-written cases use `logic`, `security`, `style_only`, `refactor`,
  `concurrency`, `improvable` or `degenerate`.
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

### Baseline

<!-- BASELINE-HEADLINE -->
`baseline.json`: run `base2`, agent `665c767`, `nvidia/nemotron-3.5-super-vl:free`, k=3, window ±3 lines, the 65
development cases (195 runs, 2026-11-04), all scored (no harness errors). The hold-out's score is
[reported apart](#hold-out-set).

| | Pass rate (per run) | pass^3 (per case) |
| :-- | --: | --: |
| **All 65 development cases** | **71%** | **52%** |
| `raise_true_bug` (36, recall) | 70% | 56% |
| `skip_false_bug` (20, precision traps) | 63% | 35% |
| `raise_nothing` (9, clean code) | 93% | 78% |
| Old file-only rule, for comparison | 68% | |

| Category | Expected action | Cases | Runs passed | Rate | Passed all 3 |
| :-- | :-- | --: | --: | --: | --: |
| concurrency | `raise_true_bug` | 3 | 9/9 | 100% | 3/3 |
| degenerate | `raise_nothing` | 2 | 6/6 | 100% | 2/2 |
| improvable | `raise_true_bug` | 3 | 4/9 | 44% | 1/3 |
| logic (synthetic) | `raise_true_bug` | 6 | 18/18 | 100% | 6/6 |
| logic (public) | `raise_true_bug` | 8 | 18/24 | 75% | 5/8 |
| mined | `raise_true_bug` | 10 | 10/30 | 33% | 0/10 |
| mined | `skip_false_bug` | 20 | 38/60 | 63% | 7/20 |
| refactor | `raise_nothing` | 3 | 7/9 | 78% | 1/3 |
| security | `raise_true_bug` | 6 | 17/18 | 94% | 5/6 |
| style_only | `raise_nothing` | 4 | 12/12 | 100% | 4/4 |

| Severity of the case's finding | Expected action | Cases | Runs passed | Rate | Passed all 3 |
| :-- | :-- | --: | --: | --: | --: |
| high | `raise_true_bug` | 9 | 19/27 | 70% | 5/9 |
| medium | `raise_true_bug` | 15 | 35/45 | 78% | 9/15 |
| low | `raise_true_bug` | 12 | 22/36 | 61% | 6/12 |
| high | `skip_false_bug` | 5 | 13/15 | 87% | 3/5 |
| medium | `skip_false_bug` | 9 | 14/27 | 52% | 2/9 |
| low | `skip_false_bug` | 6 | 11/18 | 61% | 2/6 |
| none | `raise_nothing` | 9 | 25/27 | 93% | 7/9 |

- **Failure taxonomy:** 31 `model_failure` (8 stable, 23 flaky), 0 `harness_error`, **24 `label_error`s fixed**
  (`label_errors_fixed`: 24 mined cases whose line was not the line of the code they describe at `raised_sha`). The fixes followed the code, not the results, and moved individual cases both ways: 34 cases passed all
  3 repeats before them and 34 after.
- **The 31:** of the 13 `skip_false_bug` failures, 8 re-raise the dismissed claim and 5 are the line-matching limit only
  (a *different* claim inside the window). The 16 `raise_true_bug` failures are mostly misses; in 5 the claim is raised
  in some repeats but at a line outside the window or below the required severity (`R36-d2309a5-001`, `R45-fee8a9a-004`,
  `-006`, `pub-mikro-orm-481`, `sec-002`). The 2 `raise_nothing` failures flag behaviour a refactor did not change. Each
  reason is in `error_analysis.json`.
- **Recall on mined bugs is lower than 33%.** 8 of its 10 passing runs pass only because a *different* claim is inside
  the window: from reading them, the agent raised the real claim at the line and severity the case requires twice in 30
  runs. The window also favours `run_eval.sh`, where three cases sit within lines 34 to 39. The judge (PMAT-311) is
  what separates these.

### Hold-out set

`holdout.json` lists 15 of the 80 cases (19%) that tuning never sees, so they can still check a change that was tuned
on the other 65, the **development cases**. Normal runs, the baseline, `compare_baseline.py` and PMAT-312's gate use the
development cases only.

**Run it only when deciding whether to ship an agent change.** Seeing its score after each experiment would make it
another tuning target.

```bash
eval/scripts/run_eval.sh --holdout --model nvidia/nemotron-3.5-super-vl:free --run <name>
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
  sides. Each takes about 25% of its cases, at least 1, in whole units.
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

A second model (`JUDGE_MODEL`) grades each review on three dimensions, 1 to 5, each with its reason first: **correctness**
(are the findings real?), **coverage** (did it catch what matters, against the case's `expected` and `notes`?) and
**actionability** (could a developer act on it?). It also gives a **same-claim** verdict: did a finding make the case's
claim? That separates what the line window can't: a different bug near the line counts for the window, not for the judge.

**It never gates.** `judge_results` always passes, so a case's pass or fail, `baseline.json` and the comparison don't
change. A judge error is recorded, not failed. A review with no findings has `null` correctness and actionability.

It is an LLM judge in a python assert, not Promptfoo's built-in `llm-rubric` (owner, PMAT-311), because everything sent to
a model has to go through `ReviewSession`, so an excluded file's content never reaches it
(`tests/test_canary.py`), and one call has to return all three scores and the verdict. Hold-out cases never reach it.

Set `JUDGE_MODEL`, `JUDGE_URL` and `JUDGE_TOKEN` in `.env`. A missing one stops the run before any review.

```bash
eval/scripts/run_eval.sh --stored ~/.mesh/eval-runs/PMAT-310-base2 --run judge1   # judge stored reviews; the agent doesn't run
uv run python eval/scripts/judge_report.py eval/runs/judge1/results.json           # means, proposal, line window vs same claim, cost
JUDGE_LIVE=1 uv run pytest eval/tests/test_judge.py -k live                        # the checks that need the real model
```

A normal `run_eval.sh` run judges the agent's fresh reviews too. A `--stored` run has no `run.json`, so it can't become the baseline.

**Aligning the judge (meta-evaluation):** the owner scores 25 development cases blind, and the judge's scores are compared
with theirs (**judge–human agreement**: within ±1 point per dimension, and Cohen's kappa on pass/fail, score >= 4).

```bash
uv run python eval/scripts/judge_labelling_sheet.py ~/.mesh/eval-runs/PMAT-310-base2   # sheet and labels file; no scores shown
uv run python eval/scripts/judge_report.py eval/runs/judge1/results.json --write-scores  # after the labels are committed
uv run python eval/scripts/judge_agreement.py eval/judge/human_labels.json eval/judge/judge_scores.json
```

The sample (`scripts/judge_sample.py`, seed 311) is drawn from the case files only, per group, from the development
cases. Under about 75% on a dimension: read the disagreements, tighten the rubric in a new version (`rubric.v2.md`), and
record the iteration here. Changing `JUDGE_MODEL` or the rubric means aligning again.

<!-- JUDGE-RESULTS -->
**Judge–human agreement:** not yet measured; the owner's labels come first.

**Pinning:** `z-ai/glm-5.3-flash` is an OpenRouter alias (canonical slug `z-ai/glm-5.3-flash-20260826`). A request for the
dated slug works, but responses report the alias either way, so the API can't confirm the build. OpenRouter also routes
calls to different upstream providers, three in three calls in one test. Each judgment records the requested and
reported model, the provider, its tokens and cost.

**Family:** `.env` has `CODE_REVIEW_MODEL=z-ai/glm-5.3-flash`, the judge's own family (owner decision to keep it for now,
as the stored run's reviewer is Nemotron). The warning at the start of a run checks the run's model and `CODE_REVIEW_MODEL`;
a run reviewed by that family is grading itself, and the judge must then move family and be aligned again.

### Terms

| Term | What it is | Changes when |
| :-- | :-- | :-- |
| **The agent** | The review agent, the thing being evaluated | With its code |
| **Running the agent** | `run_review_agent`: the slow step that produces a review | |
| **Stored review** | A review saved from an earlier run | Never |
| **The judge** | The separate model that scores a review | With `JUDGE_MODEL` or the rubric |
| **Judge–human agreement** | How closely the judge's scores match the owner's on the same reviews | Each alignment |
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

- **One review per commit and repeat.** Several cases share a commit. Within one repeat they share one review
  (they queue on a lock per commit, which also runs the repeats of a commit one after another); each repeat
  is a fresh review, so pass^k measures something.
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
eval/scripts/run_eval.sh --model nvidia/nemotron-3.5-super-vl:free --run base1        # k=3, every development case
eval/scripts/run_eval.sh --model nvidia/nemotron-3.5-super-vl:free --run base1        # again: resumes
eval/scripts/run_eval.sh --k 1 --run smoke --filter-pattern '^R37-1fbb920-001$'       # one case (.env model)
uv run python eval/scripts/compare_baseline.py eval/runs/base1/results.json           # vs baseline.json
uv run python eval/scripts/baseline.py eval/runs/base1                                # write a new baseline
PROMPTFOO_CONFIG_DIR=eval/runs/base1/.promptfoo promptfoo view                        # browse (Node 24)
```

`run_eval.sh` records the run's commit, model, date, k, window and `dirty` (uncommitted changes in `src/`, `eval/promptfoo/` or `eval/cases/`: such a run never becomes the baseline) in `runs/<run>/run.json`. One run folder
holds one model's reviews (the checkpoint key has no model), so a new model needs a new `--run`. A resumed run
keeps the commit and date it started at (so label fixes made between passes are counted), and is refused if
the agent changed since, or with another model or k (`scripts/run_meta.py`).

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
  bug inside the window counts as the same one. The judge's same-claim verdict is reported beside it (`judge_report.py`).
- **The PR description is today's**, fetched at run time; it may have been edited since `raised_sha`.
- **Mined cases leave out large commits** (`--max-files 22`): 3 real and 19 false findings on 7 commits.
- **Long reviews can time out in Promptfoo.** The repeats of one commit queue on its lock, so the last
  repeat of a large review (86 files: about an hour each) can wait past the provider's 2-hour timeout and
  become a `harness_error`. Rerunning the same `--run` finishes it from the checkpoints.
- **The free models' daily quota** (OpenRouter `429 free-models-per-day`) does not cover a k=3 baseline in
  one day: rerun the same `--run` after it resets (00:00 UTC).
