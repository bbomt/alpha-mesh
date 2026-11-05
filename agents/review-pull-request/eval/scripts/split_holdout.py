"""Draw the hold-out set into eval/holdout.json (PMAT-318). See "Hold-out set" in eval/README.md.

    uv run python eval/scripts/split_holdout.py [--seed N]

Never reads a run's results: the draw must not depend on how the agent did.
"""

import argparse
import collections
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "promptfoo"))
import case_rules  # noqa: E402

SEED = 318  # the ticket number, fixed before any failure was read
SHARE = 0.25
# Their failures went through error analysis on run base1, so they are not blind.
NOT_BLIND = {"R36-812f904-001", "R36-812f904-002", "R36-812f904-003", "R36-c3e9e57-001",
             "R36-d2309a5-001", "R36-d2309a5-002", "R37-1fbb920-001", "R37-1fbb920-002"}
# A smoke run showed their results before the split, so they are drawn only when nothing else fits.
RESULT_SEEN = {"sec-001", "deg-001", "deg-002", "deg-003"}


def unit_of(case: dict) -> str:
    """A whole PR goes to one side. A case with no PR (synthetic) is its own unit."""
    return f"{case['repo']}#{case['pr_number']}" if "pr_number" in case else case["case_id"]


def draw(cases: list[dict], seed: int) -> list[str]:
    """The sorted hold-out case IDs: per source and category, about SHARE of its cases, in whole units.

    Units are ordered by a hash of seed and unit, not with `random`, so the result never depends on the
    Python version. A unit that would pass the category's target is skipped, and the next one tried."""
    strata = collections.defaultdict(lambda: collections.defaultdict(list))
    for case in cases:
        strata[case["source"], case["category"]][unit_of(case)].append(case["case_id"])

    drawn = []
    for units in strata.values():
        target = max(1, round(SHARE * sum(map(len, units.values()))))
        eligible = {u: ids for u, ids in units.items() if not NOT_BLIND & set(ids)}
        order = sorted(eligible, key=lambda u: (bool(RESULT_SEEN & set(eligible[u])),
                                                hashlib.sha256(f"{seed}:{u}".encode()).hexdigest()))
        taken = []
        for unit in order:
            if len(taken) + len(eligible[unit]) <= target:
                taken += eligible[unit]
        drawn += taken or min(eligible.values(), key=len)  # every category is in the hold-out
    return sorted(drawn)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Draw the hold-out set into eval/holdout.json.")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args(argv)
    cases = case_rules.load_cases()
    held = draw(cases, args.seed)
    case_rules.HOLDOUT.write_text(json.dumps({"seed": args.seed, "cases": held}, indent=2) + "\n")
    print(f"Wrote {case_rules.HOLDOUT.name}: {len(held)} of {len(cases)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
