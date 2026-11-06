"""Draw the 25 development cases the owner hand-scores to align the judge (PMAT-311).

    uv run python eval/scripts/judge_sample.py     # writes eval/judge/sample.json

Drawn from the case files alone, in the order of a hash of seed and case ID, so no score can steer it. Cases come
from the development cases only: scoring a case means reading its review, so a hold-out case is never in it.
Mined cases share few commits, so each next one comes from the commit with the fewest so far, to read fewer
reviews twice.
"""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "promptfoo"))
import case_rules  # noqa: E402

SAMPLE = case_rules.EVAL / "judge" / "sample.json"
SEED = 311
REPEAT = 0  # the stored review scored for each case
QUOTA = {"mined/skip_false_bug": 6, "mined/raise_true_bug": 5, "public": 3, "security": 3, "logic": 2, "style_only": 2,
         "concurrency": 1, "improvable": 1, "refactor": 1, "degenerate": 1}  # owner, 2026-11-06


def group(case: dict) -> str:
    if case["category"] == "mined":
        return f"mined/{case['expected']['action']}"
    return "public" if case.get("source") == "public_pr" else case["category"]


def _rank(case: dict) -> str:
    return hashlib.sha256(f"{SEED}:{case['case_id']}".encode()).hexdigest()


def draw(cases: list[dict]) -> list[str]:
    """The sample's case IDs, sorted."""
    held = case_rules.holdout_ids()
    pools: dict[str, list[dict]] = {}
    for case in sorted((c for c in cases if c["case_id"] not in held), key=_rank):
        pools.setdefault(group(case), []).append(case)
    chosen, on_commit = [], {}
    # Largest quota first, so the mined groups share out their commits before the rest.
    left = dict(QUOTA)
    while any(left.values()):
        name = max(left, key=lambda g: (left[g], g))
        best = min(pools[name], key=lambda c: on_commit.get(c["raised_sha"], 0))  # min keeps the hash order on a tie
        pools[name].remove(best)
        on_commit[best["raised_sha"]] = on_commit.get(best["raised_sha"], 0) + 1
        chosen.append(best["case_id"])
        left[name] -= 1
    return sorted(chosen)


def main() -> int:
    SAMPLE.parent.mkdir(exist_ok=True)
    SAMPLE.write_text(json.dumps({"seed": SEED, "repeat": REPEAT, "cases": draw(case_rules.development_cases())},
                                 indent=2) + "\n")
    print(f"Wrote {SAMPLE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
