# Calibration and model decisions (review-pull-request evals)

Why the evals use the models they do, how the judge was checked, and what each run costs. Decisions are the
owner's (Team Lead) unless marked otherwise; the evidence is in Jira PMAT-311 and PMAT-326. Newest decision wins.
Prices are OpenRouter's, per million tokens, on the date given.

## Terms

| Term | Meaning |
|---|---|
| **The agent** | The review agent: the thing being evaluated. |
| **The judge** | The model that scores every review of every eval run on the rubric (correctness, coverage, actionability, 1–5). |
| **Calibration Labels** | Trusted scores on the frozen 25-case sample: the yardstick for the judge. **Gold**: a human's score (`judge/gold-human-calibration.json`); **silver**: a strong model of another family (`judge/silver-ai-calibration.json`). Per case and dimension, gold wins where set. |
| **Judge Calibration** | Running the judge on the 25 sample reviews and comparing its scores with the Calibration Labels. |
| **Judge Alignment** | The result: per dimension, the share of cases within ±1 point, and Cohen's kappa on pass/fail (score ≥ 4). The bar is about 75% within ±1. |

The labeller runs rarely, on 25 reviews, so it can be expensive and careful. The judge runs on every review, so it
must be cheap and consistent. Judge Calibration is what shows the cheap judge grades like the careful labeller.

## Current choices (2026-11-12)

| Role | Model | Where | `.env` |
|---|---|---|---|
| Agent (eval runs) | `z-ai/glm-5.3-flash` | OpenRouter | `[model] review` in `review_config.toml` / `--model` |
| Judge | `minimax/minimax-m3.1-flash` | OpenRouter, pinned to the **Minimax** host, no fallback | `EVAL_JUDGE_MODEL`, `EVAL_JUDGE_URL`, `EVAL_JUDGE_TOKEN`, `EVAL_JUDGE_PROVIDER` |
| Silver labeller | `openai/gpt-6.1-sol` (effort high, 3 passes, median) | OpenRouter | `SILVER_CALIBRATION_MODEL`, `_URL`, `_TOKEN` |

Each role is a different family, so no model grades its own family's output.

## Decisions

### 1. Silver labels until a human scores (2026-11-10, PMAT-326)

The owner has no time to score 25 cases by hand. A model of a third family scores them on the same rubric, `why`
before `score`. Human (gold) scores override silver per case and dimension when added, computed when agreement runs,
never stored. Humans score blind. Caveat: the labeller and the judge read the same rubric, so an ambiguity both read
the same way is invisible until gold scores exist.

### 2. Sol replaces Terra as the labeller (2026-11-12)

Terra's three passes disagreed with each other too often for a yardstick. Against Terra, the DeepSeek V4 Pro judge
looked borderline on correctness (75%); against Sol it reached 85%: most of the gap was Terra's noise.

| Labeller | Correctness exact / ±1 between its passes | Coverage ±1 | Actionability ±1 | Unstable cases | Cost (3 passes) |
|---|---|---|---|---|---|
| `openai/gpt-6.1-terra` | 60% / 85% | 84% | 100% | 6 of 25 | $0.90 |
| `openai/gpt-6.1-sol` | 85% / 100% | 96% | 100% | 1 of 25 | $1.58 |

Terra vs Sol: correctness 85% within ±1 (kappa 0.47), coverage 88% (0.74), actionability 100%.

### 3. The judge: minimax-m3.1-flash (2026-11-12)

History: `z-ai/glm-5.3-flash` (initial, 2026-10-29; an unpinned alias) → DeepSeek V4 Pro on DeepSeek's own API
(2026-11-11) → `minimax/minimax-m3.1-flash` (2026-11-12). The dev loop runs the evals often, so the judge's cost per
run matters as much as its alignment.

Judge Calibration against Sol (base3 repeat 0, the 25-case sample, 3 passes per candidate, median; OpenRouter's
default routing):

| Judge | Correctness ±1 / kappa | Coverage ±1 / kappa | Self ±1 (correctness) | $/call | Errors |
|---|---|---|---|---|---|
| `deepseek-v4-pro` (DeepSeek API, 1 pass) | 85% / 0.63 | 100% / 1.00 | n/a | ~$0.020 | 0 |
| **`minimax/minimax-m3.1-flash`** | 90% / **0.86** | 100% / 1.00 | **95%** | **$0.0036** | **0 / 75** |
| `z-ai/glm-5.3-flash` | 100% / 0.86 | 96% / 0.90 | 90% | $0.0051 | 2 / 75 |
| `deepseek/deepseek-v4.1-flash-1017` | 90% / 0.74 | 100% / 1.00 | 90% | $0.0050 | 2 / 75 |
| `xiaomi/mimo-v2.6-flash` | 75% / 0.34 | 100% / 1.00 | 85% | $0.0023 | 0 / 75 |

Actionability was 100% within ±1 for every judge, with kappa undefined (almost every review passes), so this sample
doesn't separate judges on it. Correctness covers 20 cases: 10 points is 2 cases. Errors were empty or non-JSON
replies.

- **Why minimax:** it ties for the best kappa, is the most consistent with itself, had no errors, is the cheapest
  of the good candidates, and cached the most prompt (63% of prompt tokens).
- **Not glm:** it is the eval agent's own model, so it would grade its own family.
- **Not `openai/gpt-6-luna`** (not run): the labeller's family; judge and labeller would share blind spots and look
  more aligned than they are.
- **Not mimo:** lowest kappa. **Not v4.1-flash:** lower kappa, and errors.
- **Not Claude:** no API access; a subscription through `claude -p` would spend the owner's interactive limits, report
  no cost or tokens, fix no temperature, and add a path to a model outside `ReviewSession`. On OpenRouter Sonnet 5.5
  costs $2 in and $10 out, over 10× minimax.
- **Not DeepSeek V4 Pro:** about 5× minimax's cost per call, and lower alignment. Its own API serves an undated name
  (`deepseek-v4-pro`) that DeepSeek can move to a new build without notice.

### 4. One host, no fallback (2026-11-12)

OpenRouter spreads a model's calls across hosts at different quantisations (minimax ran on 3; glm on 10), so two
judgments of one review could come from different weights. `EVAL_JUDGE_PROVIDER=Minimax` sends every judge call
to MiniMax's own host with `allow_fallbacks: false`.

- **No fallback:** a fallback would silently swap in another host's weights mid-run, which is what the pin
  prevents. A down host gives judge errors, which the report counts; rerun later. Unpin only knowingly, and the
  report's provider list shows what served each run.
- **Why MiniMax's host:** the vendor's own weights. On OpenRouter it costs $0.15 in, $0.60 out, $0.015 cached. Cheaper
  hosts exist (GMICloud fp8, $0.112 / $0.45) but are quantised.
- **Measured on the pinned host** (3 passes, every call served by Minimax): the same alignment as unpinned
  (correctness 90% / kappa 0.86 for the median, 85% / 0.74 for one pass; coverage 100%; self ±1 95%), 1 empty
  reply in 75, at $0.0050 per judgment: about 40% more than the unpinned run's $0.0036, which went to cheaper or
  better-caching hosts. Consistency of weights was judged worth that.
- **Cheaper hosts, calibrated (2026-11-12):** all hosts list the same price except fp8 GMICloud, and the difference is
  how much each writes (Minimax ~6.1k output tokens per judgment, the others ~3.7k). Judged 3 passes each against Sol:

  | Host | Correctness ±1 / kappa | Self ±1 | $/judgment | Errors |
  |---|---|---|---|---|
  | Minimax (chosen) | 90% / 0.86 | 95% | $0.0050 | 1 / 63 |
  | DigitalOcean | 90% / 0.74 | 95% | $0.0038 | 0 / 63 |
  | GMICloud (fp8) | 90% / 0.47 | 95% | $0.0027 | 0 / 63 |

  Every host agrees within ±1 equally; they differ on pass/fail (kappa), which is what a gate decides. GMICloud's fp8
  serving loses a lot of it. DigitalOcean is within about one pass/fail decision of Minimax for 24% less. The bar set
  beforehand was to match Minimax's kappa, so Minimax stays unless the owner prefers DigitalOcean's saving.
- **One host for every run (owner, 2026-11-12):** DigitalOcean for repeat-0 dev runs and Minimax before a merge was
  considered and rejected as too complex for about $0.07 per dev run. Hosts score differently, so it would have meant
  comparing only runs judged by the same host, and judging each baseline twice. Rule kept: never compare judge scores
  from runs judged by different hosts (the report lists each run's hosts).
- **Not MiniMax's API directly:** it lists the same price ($0.15 / $0.60 / $0.015); through OpenRouter the judge
  keeps one key and gets each call's cost and host in the reply.

### 5. Judge repeat 0 while iterating, every repeat before a merge (2026-11-11)

Like review passes (incremental while iterating, full before DONE): `run_eval.sh --judge-repeats first` (the
default) judges repeat 0 of each case only; `--judge-repeats all` judges all k before merging an agent change. The
free line checks run on every repeat either way. Recorded in alpha-mesh's `CLAUDE.md`.

**Why judge the other repeats at all, with a consistent judge:** the repeats measure **the agent's** variance, not
the judge's. The agent writes a different review each run; a judge that is consistent on one text says nothing about
the next text. Judging all repeats shows how often the agent's quality drops on a case (for example, a security bug
caught in repeat 0 but missed in repeat 2, which counts against the zero-security-miss gate) and gives a more precise
mean. While iterating, the direction of a change shows on repeat 0 across all cases; a merge decision wants the
precise mean and the worst repeats.

### 6. Cost levers that were measured, and rejected ones

- **Prompts dominate the cost:** about 15k prompt tokens per judgment (PR description, diff, files the agent read),
  about 75% of a judgment's tokens on the flash models. So lowering reasoning effort would save little. (The idea
  was proposed, then dropped once measured.)
- **Shared context first:** the prompt puts the PR description, diff and files read before the findings and the
  case, so a review's cases share a cached prefix.
- **No call where the rubric fixes the score:** a `raise_nothing` case with no findings scores coverage 5 and null
  correctness and actionability without a model call (4 of the 25 sample cases).
- **Lockfiles never reach a model:** the shipped `review_config.toml` excludes them for every repo (`*.lock`,
  `*.lockb`, `package-lock.json`, `npm-shrinkwrap.json`, `pnpm-lock.yaml`, `go.sum`), so neither the agent nor the
  judge reads them.
- **Not moved ahead: the judgment cache (PMAT-331):** it reuses a judgment only for an identical review text, and
  every new agent run writes new reviews, so a fresh run gets almost no hits.
- **Not done: `max_tokens` caps** (owner, 2026-11-11).

**What a run costs** (65 development cases, about 16% scored by the rubric without a call; estimated from the
calibration runs):

| Judge | Repeat 0 (iterating) | All 3 repeats (before a merge) |
|---|---|---|
| DeepSeek V4 Pro | ~$1.10 | ~$3.30 |
| minimax-m3.1-flash, unpinned | ~$0.20 | ~$0.60 |
| minimax-m3.1-flash, host Minimax (current) | ~$0.28 | ~$0.83 |

## When to calibrate again, and when to relabel

Two different things, with different triggers.

**Judge Calibration again** (rerun the judge on the frozen sample, about $0.31 for 3 passes; no new labels): whenever
the judge's identity changes, which is its model, host, rubric version, or what it is shown (prompt and context).
Also when a human adds gold scores, or the labeller changes. Record each result here and in `README.md`
(`<!-- JUDGE-RESULTS -->`).

**New Calibration Labels** (relabel: draw a sample, have the labeller score it, about $1.58 per 25 cases with sol,
and gold starts over for the new cases): only when the frozen sample no longer represents what the judge grades.
Calibration measures **the judge**, not the agent: labels are scores of specific review texts, and the judge's skill
at grading a review doesn't depend on which agent version wrote it. So a new baseline (base6 after base3) does not by
itself mean relabelling. Relabel when:

1. **The judge needs something the sample lacks.** Base3 has no traces, so when the judge starts reading the agent's
   trace (PMAT-325's note in `llm_rubric.py`) instead of refetching files, it can't be calibrated on base3: the
   sample moves to a base with traces.
2. **The reviews change in kind.** A major agent release, or an agent model of another family, that writes findings
   in a different style, length or format than the sample's. A drift check catches this cheaply: label 5–10 cases
   from the newest base and see whether the judge's agreement on them holds.
3. **New kinds of case.** Categories the sample doesn't cover (the sample was drawn per group).
4. **More cases make a bigger sample possible.** 25 is small: correctness covers 20 cases, so one case moves agreement
   5 points. If mining grows the development cases substantially, draw a larger sample (for example 50, about $3.20
   with sol), keeping the current 25 inside it so the results stay comparable. The hold-out stays untouched.
5. **The rubric changes in a way the old labels can't answer**, such as a new dimension or a redefined score.

Never change the sample and the judge in the same step: if agreement moves, you can't tell which change moved it.
