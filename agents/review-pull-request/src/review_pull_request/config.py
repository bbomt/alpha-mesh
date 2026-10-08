"""Configuration loading for review-pull-request.

Loads .env + review_config.toml into a Config dataclass.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from review_pull_request.tools import GitHubFileReader


REQUIRED_ENV_VARS = ["CODE_REVIEW_URL", "CODE_REVIEW_TOKEN", "CODE_REVIEW_MODEL"]

# Always excluded, even if .pr-review-ignore is missing or misplaced: token-bearing paths.
SENSITIVE_PATTERNS = ["**/.env", "**/.claude/*", "**/.mesh/*", "config/active.json"]

# Global list of files excluded from review, at the alpha-mesh root.
IGNORE_FILE = Path(__file__).resolve().parents[4] / ".pr-review-ignore"


def load_ignore_patterns(path: Path) -> list[str]:
    """Read exclusion patterns: one per line, '#' comments and blank lines skipped.

    A missing file means no extra exclusions.
    """
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as e:
        print(
            f"ERROR: could not read {path.name} ({e}). Aborting rather than reviewing "
            "files you meant to exclude; fix or remove the file.",
            file=sys.stderr,
        )
        sys.exit(1)
    patterns = []
    for raw in text.splitlines():
        line = re.sub(r"\s+#.*$", "", raw).strip()  # inline comment
        if not line or line.startswith("#"):
            continue
        if line.endswith("/"):  # gitignore-style directory: match it at any depth
            line = f"**/{line.lstrip('/')}*"
        patterns.append(line.lstrip("/") if not line.startswith("**/") else line)
    return patterns


def _load_toml(config_path: str | None) -> dict:
    """Load TOML config file, return empty dict if not found."""
    if config_path is None:
        config_path = "review_config.toml"
    p = Path(config_path)
    if not p.exists():
        return {}
    import toml

    return toml.load(str(p))


@dataclass
class Config:
    model: str
    api_url: str
    api_token: str
    overview_model: str = ""
    base_branch: str = "master"
    max_tool_calls: int = 20
    max_file_lines: int = 8000
    exclusions: list[str] = field(default_factory=list)
    output_file: str = "review_output.md"
    github_repo: str = ""

    # Runtime
    repo_root: str = ""
    verbose: bool = False
    hide_pr_comment: bool = False
    github_reader: GitHubFileReader | None = None
    # read_file results shared by every file review in one run; cleared when main() exits.
    read_cache: dict[str, str] = field(default_factory=dict)
    read_cache_max_bytes: int = 50 * 1024 * 1024
    read_cache_bytes: int = 0  # running total of read_cache contents

    @property
    def effective_overview_model(self) -> str:
        return self.overview_model or self.model


def load_config(args) -> Config:
    """Load config from .env + TOML + CLI overrides."""
    # In dry-run mode, set placeholder values so we skip .env validation.
    # The bash entry point already skips sourcing .env for --dry-run.
    if getattr(args, "dry_run", False):
        for var in REQUIRED_ENV_VARS:
            os.environ.setdefault(var, "dry-run-placeholder")

    load_dotenv()

    for var in REQUIRED_ENV_VARS:
        if not os.environ.get(var):
            print(
                f"ERROR: {var} is not set. "
                f"Copy .env.example to .env and fill in your credentials.",
                file=sys.stderr,
            )
            sys.exit(1)

    model = os.environ.get("CODE_REVIEW_MODEL", "")
    api_url = os.environ.get("CODE_REVIEW_URL", "")
    api_token = os.environ.get("CODE_REVIEW_TOKEN", "")

    if args.model:
        model = args.model
    if args.base:
        base_branch = args.base
    else:
        base_branch = "master"

    toml_config = _load_toml(args.config)

    limits = toml_config.get("limits", {})
    exclusions_section = toml_config.get("exclusions", {})
    github_section = toml_config.get("github", {})
    output_section = toml_config.get("output", {})
    models_section = toml_config.get("models", {})

    max_tool_calls = limits.get("max_tool_calls", 10)
    max_file_lines = limits.get("max_file_lines", 8000)
    exclusions = list(dict.fromkeys(
        exclusions_section.get("patterns", []) + load_ignore_patterns(IGNORE_FILE) + SENSITIVE_PATTERNS
    ))
    github_repo = github_section.get("repo", "")
    output_file = output_section.get("file", "review_output.md")
    overview_model = models_section.get("overview_model", "")

    repo_root = args.repo_path or os.getcwd()

    if not (IGNORE_FILE.parent / "bin" / "mesh").exists():
        print(
            f"WARNING: {IGNORE_FILE} is not inside an alpha-mesh checkout; "
            "the global exclusions (.pr-review-ignore) may not be applied.",
            file=sys.stderr,
        )

    return Config(
        model=model,
        api_url=api_url,
        api_token=api_token,
        overview_model=overview_model,
        base_branch=base_branch,
        max_tool_calls=max_tool_calls,
        max_file_lines=max_file_lines,
        exclusions=exclusions,
        output_file=output_file,
        github_repo=github_repo,
        repo_root=repo_root,
        verbose=getattr(args, "verbose", False),
        hide_pr_comment=getattr(args, "hide_pr_comment", False),
    )
