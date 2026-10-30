"""The harness is standalone scripts, not part of the review_pull_request package."""

import sys
from pathlib import Path

EVAL = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(EVAL / "scripts"), str(EVAL / "promptfoo"), str(EVAL.parent)]  # EVAL.parent: tests/ fakes
