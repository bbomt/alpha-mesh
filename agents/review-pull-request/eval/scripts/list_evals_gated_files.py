"""Which changed files need the eval gate (PMAT-312): the one list `mesh merge-pr` and `mesh run-evals` read.

    git diff --name-only master... | python3 eval/scripts/list_evals_gated_files.py    # prints the trigger files, if any

A trigger is a file that can change the agent's findings or how they are scored: the agent's code and prompts, its
config and dependencies, the harness and the cases. Docs, tests, the reporting scripts, the judge (it reports, never
gates) and baseline.json (a rebaseline PR, approved by the owner) are not. Standard library only: `mesh merge-pr`
runs it with the system python3.
"""

import sys

AGENT = "agents/review-pull-request/"
PREFIXES = tuple(AGENT + p for p in ("src/", "eval/promptfoo/", "eval/cases/"))
FILES = frozenset(AGENT + f for f in ("review_config.toml", "pyproject.toml", "uv.lock", "eval/promptfooconfig.yaml",
                                      "eval/holdout.json"))


def triggers(paths) -> list[str]:
    """The paths, repo-relative, that need the gate."""
    return [p for p in paths if p in FILES or p.startswith(PREFIXES)]


if __name__ == "__main__":
    print("\n".join(triggers(line.strip() for line in sys.stdin if line.strip())))
