"""The harness is a standalone script, not part of the review_pull_request package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
