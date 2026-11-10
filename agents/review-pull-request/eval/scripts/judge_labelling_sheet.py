"""The owner's sheet for hand-scoring the sample (PMAT-311), and the labels file to fill in.

    uv run python eval/scripts/judge_labelling_sheet.py ~/.mesh/eval-runs/PMAT-322-base3

Writes eval/runs/judge-labelling/sheet.md (gitignored: it holds reviews and diffs) and, if it does not exist yet,
eval/judge/gold-human-calibration.json. Scoring is blind, so the sheet shows no judge score and no line-window result. Reads the
stored run for the sampled cases only, so no hold-out review is opened.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "promptfoo")]
import agent_provider  # noqa: E402
import case_rules  # noqa: E402
import judge_sample  # noqa: E402
import llm_rubric  # noqa: E402
from review_pull_request.policy import PathPolicy  # noqa: E402
from review_pull_request.readers import PolicyFileReader  # noqa: E402
from review_pull_request.safe_diff import SafeDiff  # noqa: E402

LABELS = case_rules.EVAL / "judge" / "gold-human-calibration.json"
OUT = case_rules.EVAL / "runs" / "judge-labelling" / "sheet.md"
TODO = "TODO"


def template(sampled: list[tuple[dict, dict]]) -> dict:
    """One entry per case, with TODO only where a judgement is needed (the rubric fixes the rest)."""
    cases = {}
    for case, review in sampled:
        empty = not review["findings"]
        cases[case["case_id"]] = {
            "correctness": None if empty else TODO, "coverage": TODO, "actionability": None if empty else TODO, "note": ""}
    return {"rubric": llm_rubric.RUBRIC_VERSION, "repeat": judge_sample.REPEAT, "cases": cases}


def _agent_saw(context: dict) -> list[str]:
    """The same three things the judge gets. Files use a longer fence, as a read file may hold ``` itself."""
    out = ["\n**PR description:**\n", context["description"] or "(none)",
           "\n<details><summary>Diff</summary>\n", "```diff", context["diff"], "```\n", "</details>",
           "\n**Files the agent read:**" + ("" if context["files"] else " none.")]
    for path, content in context["files"].items():
        out += [f"\n<details><summary><code>{path}</code></summary>\n", "````", content, "````\n", "</details>"]
    return out


def sheet(sampled: list[tuple[dict, dict]], contexts: dict[str, dict]) -> str:
    """Cases on one commit are adjacent and share one copy of what the agent saw: {sha: {diff, description, files}}."""
    out = [f"# Hand-scoring the judge's sample: {len(sampled)} cases\n",
           f"Score each case in `eval/judge/gold-human-calibration.json`, using `eval/judge/rubric.{llm_rubric.RUBRIC_VERSION}.md`: "
           "1 to 5 per dimension. Replace every `TODO`; `null` is already filled in where "
           "the rubric says so. Add a `note` when a score was a close call.\n"]
    shown = set()
    for case, review in sorted(sampled, key=lambda s: (s[0]["raised_sha"], s[0]["case_id"])):
        sha = case["raised_sha"]
        out += [f"\n---\n\n## {case['case_id']}\n",
                f"[Commit diff on GitHub](https://github.com/{case['repo']}/compare/{case['base_sha']}...{sha})\n",
                "**Case (ground truth):**\n", "```json",
                json.dumps({"expected": case["expected"], "finding": case.get("finding"), "category": case["category"],
                            "notes": case.get("notes"), "label_reason": case.get("label_reason")}, indent=1), "```\n"]
        out.append(f"**The review's {len(review['findings'])} finding(s):**\n")
        for i, f in enumerate(review["findings"]):
            out.append(f"{i}. `{f['file']}:{f['line']}` ({f['severity']}): {f['summary']}"
                       + (f"\n   - Suggestion: {f['suggestion']}" if f.get("suggestion") else ""))
        if sha in shown:
            out.append(f"\nThe description, diff and files are shown above, under the first case on `{sha[:7]}`.")
        else:
            shown.add(sha)
            out += _agent_saw(contexts[sha])
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Write the owner's scoring sheet for the judge's sample.")
    parser.add_argument("run_dir", type=Path, help="the stored run, with results.json")
    args = parser.parse_args(argv)
    sample = set(json.loads(judge_sample.SAMPLE.read_text())["cases"])
    config, token = llm_rubric.judge_config(), llm_rubric.github_token()
    policy = PathPolicy(config.exclusions)
    results = str(args.run_dir / "results.json")
    sampled, contexts = [], {}
    for case in case_rules.development_cases():
        if case["case_id"] not in sample:
            continue
        review = agent_provider._stored_review(results, case["case_id"], judge_sample.REPEAT)
        review = {**review, "findings": [f for f in review["findings"] if not policy.is_excluded(f["file"])]}
        sampled.append((case, review))
        if case["raised_sha"] not in contexts:
            owner, repo = case["repo"].split("/")
            diff = SafeDiff.from_raw(llm_rubric.get_diff(owner, repo, case["base_sha"], case["raised_sha"], token), policy)
            reader = PolicyFileReader(llm_rubric.file_reader(case, token), policy)
            try:
                context = llm_rubric.agent_context(case, review, reader, policy, config.max_file_lines, token)
            finally:
                reader.close()
            contexts[case["raised_sha"]] = {"diff": diff.text, **context}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(sheet(sampled, contexts))
    print(f"Wrote {OUT}")
    if not LABELS.exists():
        LABELS.write_text(json.dumps(template(sampled), indent=2) + "\n")
        print(f"Wrote {LABELS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
