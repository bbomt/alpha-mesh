# Evals for review-pull-request

The golden dataset (the cases, below) and the Promptfoo harness that runs the agent on it
([Running the evals](#running-the-evals)).

## What the dataset is

High and medium review findings from merged PRs, each labelled by what happened to its ID
(`R<PR>-<commit7>-<NNN>`, on the finding's heading):

- **Real issue:** a commit in the PR cites the ID, e.g. `fix[PMAT-12]: fixed review finding R31-e0c98f3-003 in src/x.py`.
- **False positive:** a comment on the PR dismisses it on a line of its own:
  `Dismissed review finding R31-e0c98f3-004: <reason>`. If an ID is dismissed more than once, the last
  line wins, so a wrong dismissal is corrected by posting a new line.

Everything else is not a case: duplicates (a dismissal reason starting `duplicate of <ID>`), conflicts
(both cited and dismissed), unresolved findings (neither), findings without an ID or a line, and low findings.
Running the agent against the dataset is [Running the evals](#running-the-evals).

## How to run it

From `agents/review-pull-request`. The only prerequisite is `gh auth login`. No model is called, and
nothing is written to GitHub.

```bash
S=eval/scripts/eval_cases.py
python3 $S mine --repo bbomt/alpha-mesh --limit 40            # the 40 most recently merged PRs
python3 $S mine --repo bbomt/alpha-mesh --prs 36,37           # only some PRs
python3 $S label R31-e0c98f3-004 real_issue --reason "The caller never checks the length (src/x.py:40)."
```

`mine` prints a summary: `cited`, `dismissed`, `duplicate`, `conflict`, `unresolved`, `no_id`, `no_line`.

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
| `scripts/eval_cases.py` | the miner (stdlib only) |
| `schema/case.schema.json` | the JSON Schema every case file must match |
| `promptfooconfig.yaml`, `promptfoo/` | the harness: provider, eval adapter, loader, asserts, `case_rules.py` |
| `scripts/run_eval.sh`, `scripts/baseline.py`, `scripts/compare_baseline.py` | run, baseline, compare |
| `schema/review.schema.json`, `schema/baseline.schema.json` | the provider's output, the baseline's guard |
| `baseline.json`, `error_analysis.json` | the committed baseline and the error analysis behind it |
| `runs/` | eval runs (gitignored) |
| `tests/` | acceptance tests with a fake `gh`, a fake GitHub and a fake model |

A case applies to `raised_sha`, the commit the review saw. `head_sha` is the PR's final commit, kept
for reference.

## What a case expects

Besides the finding and its label, every case has:

- `category`: `mined` for mined cases. Hand-written cases (later) use `logic`, `security`, `style_only`,
  `refactor`, `concurrency`, `improvable` or `degenerate`.
- `notes`: free text for people, saying why the case exists. The harness never reads it; the judge may.
- `expected`: what a good review of `raised_sha` does, never expected text. Every case passes or fails.
  `action` and `flag_file` are required:

| `action` | The review must | Other keys | `mine` writes it for |
| :-- | :-- | :-- | :-- |
| `raise_true_bug` | raise a finding in `flag_file` within ±3 lines of `finding.line`, at `min_severity` or above | `min_severity` (required), `must_mention_any` (optional, added by hand: the finding's text mentions at least one of these strings, ignoring case) | `real_issue` |
| `skip_false_bug` | raise no finding in `flag_file` within ±3 lines of `finding.line` (a hallucination); other findings, in that file too, are fine | none | `false_positive` |
| `raise_nothing` | raise no finding in `flag_file`, which may be a glob (hand-written clean-code cases) | `min_severity` (optional: findings below it do not count; without it, any finding counts, low included) | never |

- `flag_file` is a repo-relative path. Only `raise_nothing` takes a glob, with git rules: `*` and `?` stay
  within one folder, `**` spans any depth, and the glob matches the whole path (`src/*.py` matches
  `src/a.py`, not `src/sub/a.py`; `agents/**/*.py` matches every `.py` under `agents/`).
- **A true or false bug is matched by its line.** The review runs on the frozen code of `raised_sha`, the
  commit the finding was raised on, so `finding.line` is a stable anchor: the question is whether the review
  raised a finding in the same part of the file (owner, PMAT-304). The window is `WINDOW` in
  `promptfoo/case_rules.py` (±3 lines: the widest that keeps apart the two `orchestrator/cli.py` pairs, lines
  135 / 140 and 151 / 155, where one review raised a real and a false bug in one file). So every
  `raise_true_bug` and `skip_false_bug` case must have `finding.line` (the schema enforces it).

`uv run pytest eval/tests` checks every case file against `schema/case.schema.json` (a missing or unknown
key, a glob outside `raise_nothing`, or a true or false bug without a line, fails), and rejects a `raise_nothing` case whose `flag_file` matches
another case's file at the same repo and `raised_sha`: a file cannot be both clean and hold a finding.

## Running the evals

`promptfoo eval` runs the real agent on every case's commit and checks its findings against the case's
`expected` block (PMAT-304). `promptfoo view` shows the results.

### Baseline

<!-- BASELINE-HEADLINE -->

### Terms

| Term | What it is | Changes when |
| :-- | :-- | :-- |
| **Golden dataset** (labelled dataset, eval set) | The cases: input + **ground truth** (`real_issue` / `false_positive`, and the `expected` block) | A **label error** is found and fixed |
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
same review core, `review_commit(owner, repo, base, head, description)`. The eval adapter posts nothing. It
reads the findings (file, line, severity, summary) from the review's final graph state, the data the PR
comment is rendered from, so nothing parses markdown. The provider (`promptfoo/agent_provider.py`) returns
them as JSON (`schema/review.schema.json`; extra fields are allowed, for later asserts).

- **One review per commit and repeat.** Several cases share a commit. Within one repeat they share one review
  (they queue on a lock per commit); each repeat is a fresh review, so pass^k measures something.
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
eval/scripts/run_eval.sh --model nvidia/nemotron-3.5-super-vl:free --run base1        # k=3, every case
eval/scripts/run_eval.sh --model nvidia/nemotron-3.5-super-vl:free --run base1        # again: resumes
eval/scripts/run_eval.sh --k 1 --run smoke --filter-pattern '^R37-1fbb920-001$'       # one case (.env model)
uv run python eval/scripts/compare_baseline.py eval/runs/base1/results.json           # vs baseline.json
uv run python eval/scripts/baseline.py eval/runs/base1                                # write a new baseline
PROMPTFOO_CONFIG_DIR=eval/runs/base1/.promptfoo promptfoo view                        # browse (Node 24)
```

`run_eval.sh` records the run's commit, model, date, k and window in `runs/<run>/run.json`. One run folder
holds one model's reviews (the checkpoint key has no model), so a new model needs a new `--run`.

### Error analysis

After a run, read every failing case (`compare_baseline.py` lists the new ones; `baseline.py` refuses to
write until each failing case is classed) and record why it failed in `error_analysis.json`:

```json
{"R36-812f904-001": {"failure_class": "model_failure", "reason": "Re-raises the dismissed save_queue ordering claim at cli.py:135."}}
```

- `model_failure`: the agent got it wrong (re-raised the false finding, missed the real one). Leave it
  failing: this is what the eval measures. **When in doubt, it stays a `model_failure`**; a disputed case
  stays failing and is listed for the owner to decide.
- `label_error`: the ground truth is wrong, not the agent. Fix the case: a wrong label with
  `eval_cases.py label <ID> real_issue --reason "..."`, or a wrong `expected` by hand, with the reason in
  `notes`.
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

### Known limitations

- **Selection bias.** Mined cases were selected by the model that raised them, so they favour or penalise
  that model. Compare agent versions on the full set; compare models on the synthetic and public cases
  (PMAT-310). Each case's `model` field records its source; no per-model split is reported.
- **Line matching is a proxy** until the judge (PMAT-311): the same claim cited outside the window (at a
  call site, say) is missed, and a different bug inside the window counts as the same one.
- **The PR description is today's**, fetched at run time; it may have been edited since `raised_sha`.
- **49 of the cases are legacy** (`legacy-file-rule/`): all `false_positive`, labelled by the weaker file
  rule. They are scored with the rest; their `category`/folder tells them apart.
- **No hold-out set yet** (PMAT-310, when synthetic cases arrive).
