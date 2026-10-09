"""Architecture rules that keep the single choke points from being bypassed."""

import ast
import dataclasses
import re
from pathlib import Path

from review_pull_request.config import Config

SRC = Path(__file__).resolve().parents[1] / "src" / "review_pull_request"


def _source(name):
    return (SRC / name).read_text(encoding="utf-8")


STAGE_FORBIDDEN = {"_inner", "open", "read_text", "read_bytes", "Path", "os",
                   "llm", "HttpLLM", "GitHubFileReader", "LocalFileReader", "github_reader",
                   "httpx", "requests", "urllib", "socket", "subprocess", "importlib", "__import__"}


def _identifiers(path):
    """Every name a module mentions: imports, names and attribute accesses, anywhere in the file."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(part for a in node.names for part in a.name.split("."))
            names.update(a.asname for a in node.names if a.asname)
            if isinstance(node, ast.ImportFrom) and node.module:
                names.update(node.module.split("."))
    return names


def test_stages_cannot_reach_the_model_or_files_except_through_the_session():
    stages = sorted(SRC.rglob("stage*.py"))
    assert stages, "no stage modules found; the rule below would check nothing"
    for stage in stages:
        offending = _identifiers(stage) & STAGE_FORBIDDEN
        assert not offending, f"{stage.name} uses {sorted(offending)}"


def test_the_config_stages_receive_holds_no_unguarded_reader():
    fields = {f.name for f in dataclasses.fields(Config)}
    assert not {n for n in fields if "reader" in n}, "a raw reader on Config bypasses PolicyFileReader"


def test_path_comparison_lives_only_in_the_policy():
    for module in SRC.rglob("*.py"):
        if module.name == "policy.py":
            continue
        assert "fnmatch" not in module.read_text(encoding="utf-8"), f"{module.name} compares paths itself"


def test_safe_diff_is_only_built_in_its_own_module():
    for module in SRC.rglob("*.py"):
        if module.name == "safe_diff.py":
            continue
        assert not re.search(r"\bSafeDiff\(", module.read_text(encoding="utf-8")), f"{module.name} builds a SafeDiff directly"


def test_only_the_session_and_guard_modules_talk_to_the_http_client():
    for module in SRC.rglob("*.py"):
        if module.name in ("llm.py", "session.py", "guard.py"):
            continue
        assert "HttpLLM" not in module.read_text(encoding="utf-8"), f"{module.name} uses HttpLLM"
