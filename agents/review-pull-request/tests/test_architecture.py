"""Architecture rules that keep the single choke points from being bypassed."""

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "review_pull_request"


def _source(name):
    return (SRC / name).read_text()


def test_stages_cannot_reach_the_model_or_files_except_through_the_session():
    forbidden = re.compile(r"\b(chat|HttpLLM|execute_read_file|GitHubFileReader|LocalFileReader)\b")
    for stage in ("stage1.py", "stage2.py"):
        imports = [l for l in _source(stage).splitlines() if l.startswith(("import ", "from "))]
        offending = [l for l in imports if forbidden.search(l)]
        assert not offending, f"{stage} imports {offending}"


def test_path_comparison_lives_only_in_the_policy():
    for module in SRC.glob("*.py"):
        if module.name == "policy.py":
            continue
        assert "fnmatch" not in module.read_text(), f"{module.name} compares paths itself"


def test_safe_diff_is_only_built_in_its_own_module():
    for module in SRC.glob("*.py"):
        if module.name == "safe_diff.py":
            continue
        assert not re.search(r"\bSafeDiff\(", module.read_text()), f"{module.name} builds a SafeDiff directly"


def test_only_the_session_and_guard_modules_talk_to_the_http_client():
    for module in SRC.glob("*.py"):
        if module.name in ("llm.py", "session.py", "guard.py"):
            continue
        assert "HttpLLM" not in module.read_text(), f"{module.name} uses HttpLLM"
