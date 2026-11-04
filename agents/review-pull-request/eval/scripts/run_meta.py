"""Write runs/<run>/run.json, what baseline.py records (PMAT-304). Called by run_eval.sh:

    run_meta.py <run.json> <commit> <model> <date> <k> <dirty>

A resumed run keeps the commit and date it started at: baseline.py counts label fixes since that commit, and a
fix made between passes must count. So a resume is refused when the agent (src/, eval/promptfoo/) changed
since then, or with another model or k (the checkpoint key has neither).
"""

import json
import subprocess
import sys
from pathlib import Path

AGENT = Path(__file__).resolve().parents[2]


def agent_changed(since: str) -> bool:
    diff = subprocess.run(["git", "-C", str(AGENT), "diff", "--quiet", "--end-of-options", since, "HEAD", "--", "src",
                           "eval/promptfoo"])
    if diff.returncode > 1:
        sys.exit(f"ERROR: git diff {since} failed")
    return diff.returncode == 1


def write(path: Path, *, commit: str, model: str, date: str, k: int, dirty: bool, changed=agent_changed) -> None:
    if path.exists():
        old = json.loads(path.read_text())
        if old["model"] != model:
            sys.exit(f"ERROR: {path.parent} holds another model's reviews; use a new --run")
        if old["k"] != k:
            sys.exit(f"ERROR: {path.parent} was started with k={old['k']}; use a new --run")
        if changed(old["commit"]):
            sys.exit(f"ERROR: the agent (src/, eval/promptfoo/) changed since {path.parent} started at "
                     f"{old['commit'][:7]}; use a new --run")
        commit, date, dirty = old["commit"], old["date"], dirty or old.get("dirty", False)
    path.write_text(json.dumps({"commit": commit, "model": model, "date": date, "k": k, "window": 3,
                                "dirty": dirty}) + "\n")


if __name__ == "__main__":
    path, commit, model, date, k, dirty = sys.argv[1:]
    write(Path(path), commit=commit, model=model, date=date, k=int(k), dirty=dirty == "true")
