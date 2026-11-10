"""Silver labels (PMAT-326): a model scores the judge's frozen sample on the judge's rubric, where a human has not.

    uv run python eval/scripts/silver_labels.py ~/.mesh/eval-runs/PMAT-322-base3 --model openai/gpt-5-nano

Humans remain the gold standard (eval/judge/gold-human-calibration.json); judge_agreement.py takes a human score where
one is set, else this file's. The labeller gets what the judge and the human sheet get: the rubric, the case's ground
truth, the review's findings, the PR description, the diff and the files the agent read. It goes through llm_rubric.ask,
so through ReviewSession: an excluded file never reaches it, and secrets are redacted. It reads only the stored review
of each sampled case (repeat judge_sample.REPEAT), never a judge score, a line-window result or a hold-out case.

Each pass is one strict-JSON reply per case, `why` before `score` in every dimension. The file keeps the median of the
passes (odd, so the median is a score) and marks a case `unstable` when its passes differ by more than 1 point.
Writes eval/judge/silver-ai-calibration.json (scores only, committed; its header names the model) and
eval/runs/judge-labelling/silver-ai-calibration.md (the reasons, gitignored: it quotes reviews and code).

The model is the --model argument, sent to JUDGE_URL with JUDGE_TOKEN. A frontier model run needs the Team Lead's
sign-off (owner, 2026-11-10): develop with a cheap one.
"""

import argparse
import datetime
import json
import statistics
import sys
from dataclasses import replace
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "promptfoo")]
import agent_provider  # noqa: E402
import case_rules  # noqa: E402
import judge_sample  # noqa: E402
import llm_rubric  # noqa: E402
from review_pull_request.policy import PathPolicy  # noqa: E402

SCORES = case_rules.EVAL / "judge" / "silver-ai-calibration.json"
REASONS = case_rules.EVAL / "runs" / "judge-labelling" / "silver-ai-calibration.md"
DIMENSIONS = llm_rubric.DIMENSIONS


class LabelError(Exception):
    """A reply that can't be a label: never replaced by a default score."""


def response_schema(has_findings: bool) -> dict:
    """The strict reply schema. `why` comes before `score`, so the reasoning is written first. With no findings
    the rubric fixes correctness and actionability at null, so only coverage is asked for."""
    part = {"type": "object", "properties": {"why": {"type": "string"}, "score": {"type": "integer", "enum": [1, 2, 3, 4, 5]}},
            "required": ["why", "score"], "additionalProperties": False}
    asked = DIMENSIONS if has_findings else ("coverage",)
    return {"type": "json_schema", "json_schema": {"name": "rubric_scores", "strict": True, "schema": {
        "type": "object", "properties": {d: part for d in asked}, "required": list(asked), "additionalProperties": False}}}


def make_labeller_chat_model(meta: dict, reasoning_effort: str, schema: dict):
    """The judge's chat model, with no temperature (reasoning models refuse one), the reasoning effort and the
    strict schema. require_parameters: OpenRouter routes only to a provider that honours them."""
    return llm_rubric.make_judge_chat_model(meta, temperature=None, extra_body={
        "response_format": schema, "reasoning": {"effort": reasoning_effort}, "provider": {"require_parameters": True}})


def label_once(case: dict, review: dict, model: str, reasoning_effort: str) -> dict:
    """One pass over one case: {dimension: {why, score}, plus what the API reported (model, provider, cost)}."""
    if case["case_id"] in case_rules.holdout_ids():
        raise LabelError(f"{case['case_id']} is a hold-out case: it is never labelled")
    config = replace(llm_rubric.judge_config(), model=model)
    policy = PathPolicy(config.exclusions)
    has_findings = any(not policy.is_excluded(f["file"]) for f in review["findings"])
    meta = {}
    text, findings = llm_rubric.ask(case, review, config,
                                    make_labeller_chat_model(meta, reasoning_effort, response_schema(has_findings)))
    try:
        return {**llm_rubric._parse(text, findings), **meta}
    except llm_rubric.JudgeReplyError as e:
        raise LabelError(f"{case['case_id']}: {e}") from e


def combine(passes: list[dict]) -> dict:
    """One case's label from its passes: the median per dimension, and unstable if any dimension spreads over 1."""
    label, unstable = {}, False
    for d in DIMENSIONS:
        scores = [p[d]["score"] for p in passes]
        label[d] = None if scores[0] is None else int(statistics.median(scores))
        unstable |= label[d] is not None and max(scores) - min(scores) > 1
    return {**label, "note": "", "unstable": unstable}


def self_agreement(runs: dict[str, list[dict]]) -> dict:
    """Per dimension, the share of cases whose passes agree exactly and within 1 point."""
    out = {}
    for d in DIMENSIONS:
        spreads = [max(s) - min(s) for s in ([p[d]["score"] for p in passes] for passes in runs.values())
                   if s[0] is not None]
        out[d] = {"n": len(spreads), "exact": sum(x == 0 for x in spreads) / len(spreads) if spreads else None,
                  "within_1": sum(x <= 1 for x in spreads) / len(spreads) if spreads else None}
    return out


def _seen(runs: dict[str, list[dict]], key: str) -> list[str]:
    return sorted({str(p[key]) for passes in runs.values() for p in passes if p.get(key) is not None})


def scores_file(runs: dict[str, list[dict]], model: str, reasoning_effort: str) -> dict:
    """The committed file: human_labels' schema (rubric, repeat, cases) plus who labelled it and how."""
    return {"standard": "silver",
            "labeller": {"requested": model, "reported": _seen(runs, "model_reported"),
                         "providers": _seen(runs, "provider")},
            "reasoning_effort": reasoning_effort, "rubric": llm_rubric.RUBRIC_VERSION, "repeat": judge_sample.REPEAT,
            "passes": len(next(iter(runs.values()))), "date": datetime.date.today().isoformat(),
            "cost_usd": round(sum(p.get("cost") or 0 for passes in runs.values() for p in passes), 4),
            "self_agreement": self_agreement(runs),
            "cases": {case_id: combine(passes) for case_id, passes in sorted(runs.items())}}


def reasons(runs: dict[str, list[dict]], scores: dict) -> str:
    """The gitignored reasons: read a case here only after a human has scored it, never before."""
    out = [f"# Silver reasons: {scores['labeller']['requested']}, rubric {scores['rubric']}\n",
           "For reading a disagreement after a human has scored the case. Score blind first, from sheet.md.\n"]
    for case_id, passes in sorted(runs.items()):
        label = scores["cases"][case_id]
        out.append(f"\n## {case_id}{' (unstable)' if label['unstable'] else ''}\n")
        out.append("Median: " + ", ".join(f"{d} {label[d]}" for d in DIMENSIONS) + "\n")
        for i, p in enumerate(passes, 1):
            out.append(f"\n### Pass {i}\n")
            out += [f"- **{d}: {p[d]['score']}**: {p[d]['why']}" for d in DIMENSIONS if p[d]["score"] is not None]
    return "\n".join(out) + "\n"


def _odd(value: str) -> int:
    n = int(value)
    if n < 1 or n % 2 == 0:
        raise argparse.ArgumentTypeError("must be odd, so the median is a score")
    return n


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Silver labels for the judge's frozen sample.")
    parser.add_argument("run_dir", type=Path, help="the stored run, with results.json")
    parser.add_argument("--model", required=True, help="the labeller, an OpenRouter model ID")
    parser.add_argument("--passes", type=_odd, default=3)
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default="high")
    args = parser.parse_args(argv)
    sample = set(json.loads(judge_sample.SAMPLE.read_text())["cases"])
    cases = [c for c in case_rules.development_cases() if c["case_id"] in sample]
    if missing := sample - {c["case_id"] for c in cases}:
        print(f"ERROR: no development case for {', '.join(sorted(missing))}", file=sys.stderr)
        return 1
    results = str(args.run_dir / "results.json")
    reviews = {c["case_id"]: agent_provider._stored_review(results, c["case_id"], judge_sample.REPEAT) for c in cases}
    runs: dict[str, list[dict]] = {c["case_id"]: [] for c in cases}
    for n in range(1, args.passes + 1):
        for c in cases:
            print(f"[silver] pass {n}/{args.passes}: {c['case_id']}", file=sys.stderr, flush=True)
            try:
                runs[c["case_id"]].append(label_once(c, reviews[c["case_id"]], args.model, args.reasoning_effort))
            except LabelError as e:
                print(f"ERROR: pass {n}: {e}", file=sys.stderr)
                return 1
    scores = scores_file(runs, args.model, args.reasoning_effort)
    SCORES.write_text(json.dumps(scores, indent=2) + "\n")
    REASONS.parent.mkdir(parents=True, exist_ok=True)
    REASONS.write_text(reasons(runs, scores))
    print(f"Wrote {SCORES}\nWrote {REASONS}")
    print(f"Labeller {args.model} (reported: {', '.join(scores['labeller']['reported']) or 'n/a'}; "
          f"providers: {', '.join(scores['labeller']['providers']) or 'n/a'}), cost ${scores['cost_usd']:.4f}")
    for d, row in scores["self_agreement"].items():
        exact = "n/a" if row["exact"] is None else f"{row['exact']:.0%}"
        within = "n/a" if row["within_1"] is None else f"{row['within_1']:.0%}"
        print(f"  {d:14} passes agree exactly: {exact:>4}   within ±1: {within:>4}   (n={row['n']})")
    if unstable := [c for c, label in scores["cases"].items() if label["unstable"]]:
        print("Unstable (check these first, after scoring them blind): " + ", ".join(unstable))
    return 0


if __name__ == "__main__":
    sys.exit(main())
