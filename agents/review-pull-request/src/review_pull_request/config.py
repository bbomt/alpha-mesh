"""Configuration loading for review-pull-request.

Loads .env + review_config.toml into a Config dataclass.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# The model is not here: it is [model] review in review_config.toml, committed, so a model change is a PR (PMAT-312).
REQUIRED_ENV_VARS = ["CODE_REVIEW_URL", "CODE_REVIEW_TOKEN"]

# Always excluded, even if .pr-review-ignore is missing or misplaced: token-bearing paths.
SENSITIVE_PATTERNS = ["**/.env", "**/.env.*", "**/.envrc", "**/.claude/*", "**/.mesh/*", "**/config/active.json"]

# Global list of files excluded from review, at the alpha-mesh root.
IGNORE_FILE = Path(__file__).resolve().parents[4] / ".pr-review-ignore"
# Simple KEY=value lines only (no shell syntax); variables already in the environment win.
ENV_FILE = Path(__file__).resolve().parents[4] / ".env"


def _has_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in "*?[")


def _zero_dir_variants(pattern: str) -> list[str]:
    """'a/**/b' also matches 'a/b' in gitignore; PathPolicy needs that variant spelled out."""
    if "/**/" not in pattern:
        return [pattern]
    head, tail = pattern.split("/**/", 1)
    return [f"{head}/**/{t}" for t in _zero_dir_variants(tail)] + [f"{head}/{t}" for t in _zero_dir_variants(tail)]


def normalise_patterns(lines: list[str]) -> list[str]:
    """Turn gitignore-style lines (from .pr-review-ignore or [exclusions]) into PathPolicy patterns."""
    patterns = []
    for raw in lines:
        line = re.sub(r"\s+#.*$", "", raw).strip()  # inline comment
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            print(f"ERROR: exclusion line '{line}' uses '!' (negation), which is not supported. "
                  "Remove it; aborting rather than reviewing files you meant to exclude.", file=sys.stderr)
            sys.exit(1)
        if line.endswith("/") and line.startswith("/"):  # anchored directory: repo root only
            patterns.append(f"{line.strip('/')}/*")
        elif line.endswith("/"):  # gitignore-style directory: match it at any depth
            patterns.append(f"**/{line}*")
        elif "/" not in line and not _has_glob(line):
            # bare name: a file or a directory, at any depth (gitignore semantics)
            patterns.extend([f"**/{line}", f"**/{line}/*"])
        else:
            # contains a slash: relative to the repo root (gitignore semantics)
            line = line.lstrip("/")
            # a literal path may name a directory: cover its contents too
            for variant in _zero_dir_variants(line):
                # a glob may name a directory too ("x/*" already covers everything below x/)
                patterns.extend([variant] if variant.endswith("*") else [variant, f"{variant}/*"])
    return patterns


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
    return normalise_patterns(text.splitlines())


def _load_toml(config_path: str | None) -> dict:
    """Load the TOML config. The default sits next to this agent; a missing --config path aborts."""
    if config_path is None:
        p = Path(__file__).resolve().parents[2] / "review_config.toml"
        if not p.exists():
            return {}
    else:
        p = Path(config_path)
        if not p.exists():
            print(f"ERROR: config file not found: {config_path}. Aborting rather than reviewing "
                  "without its exclusions.", file=sys.stderr)
            sys.exit(1)
    import toml

    return toml.load(str(p))


def configured_model(role: str, config_path: str | None = None) -> str:
    """[model] <role> in review_config.toml. The one role is `review`: mesh review-pull-request uses it, and the evals
    and the gate measure it. Exits naming the setting when it is missing."""
    model = _load_toml(config_path).get("model", {}).get(role, "")
    if not isinstance(model, str) or not model:
        print(f"ERROR: [model] {role} is not set in review_config.toml. Set it there: the agent's model is "
              "committed, not in .env (PMAT-312).", file=sys.stderr)
        sys.exit(1)
    return model


@dataclass
class Config:
    model: str
    api_url: str
    api_token: str
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
    # read_file results shared by every file review in one run; cleared when main() exits.
    read_cache: dict[str, str] = field(default_factory=dict)
    read_cache_max_bytes: int = 50 * 1024 * 1024
    read_cache_bytes: int = 0  # running total of read_cache contents


def load_config(args) -> Config:
    """Load config from .env + TOML + CLI overrides."""
    dry_run = getattr(args, "dry_run", False)
    if not ENV_FILE.is_file() and not dry_run:
        print(
            f"ERROR: {ENV_FILE} not found. "
            f"Copy .env.example to .env and fill in your credentials.",
            file=sys.stderr,
        )
        sys.exit(1)

    load_dotenv(ENV_FILE)

    # In dry-run mode, placeholders fill any variable the environment and .env left unset.
    if dry_run:
        for var in REQUIRED_ENV_VARS:
            os.environ.setdefault(var, "dry-run-placeholder")

    for var in REQUIRED_ENV_VARS:
        if not os.environ.get(var):
            print(
                f"ERROR: {var} is not set. "
                f"Set it in {ENV_FILE} (see .env.example).",
                file=sys.stderr,
            )
            sys.exit(1)

    if os.environ.get("CODE_REVIEW_MODEL"):
        print("WARNING: CODE_REVIEW_MODEL in .env is no longer read: the model is [model] review in "
              "agents/review-pull-request/review_config.toml (PMAT-312). Remove it from .env.", file=sys.stderr)
    model = args.model or configured_model("review", args.config)
    api_url = os.environ.get("CODE_REVIEW_URL", "")
    api_token = os.environ.get("CODE_REVIEW_TOKEN", "")

    if args.base:
        base_branch = args.base
    else:
        base_branch = "master"

    toml_config = _load_toml(args.config)

    limits = toml_config.get("limits", {})
    exclusions_section = toml_config.get("exclusions", {})
    github_section = toml_config.get("github", {})
    output_section = toml_config.get("output", {})

    max_tool_calls = limits.get("max_tool_calls", 20)
    max_file_lines = limits.get("max_file_lines", 8000)
    toml_patterns = exclusions_section.get("patterns", [])
    if not isinstance(toml_patterns, list) or not all(isinstance(p, str) for p in toml_patterns):
        print(f"ERROR: [exclusions] patterns must be a list of strings, got {toml_patterns!r}. "
              "Aborting rather than reviewing files you meant to exclude.", file=sys.stderr)
        sys.exit(1)
    exclusions = list(dict.fromkeys(
        normalise_patterns(toml_patterns)
        + load_ignore_patterns(IGNORE_FILE) + SENSITIVE_PATTERNS
    ))
    github_repo = github_section.get("repo", "")
    output_file = output_section.get("file", "review_output.md")

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
