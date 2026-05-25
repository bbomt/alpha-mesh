"""Configuration loading for pr-review-performance.

Loads .env + review_config.toml into a Config dataclass.
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv


REQUIRED_ENV_VARS = ["CODE_REVIEW_URL", "CODE_REVIEW_TOKEN", "CODE_REVIEW_MODEL"]


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
    max_tool_calls: int = 10
    max_file_lines: int = 8000
    exclusions: list[str] = field(default_factory=list)
    output_file: str = "review_output.md"
    github_repo: str = ""
    github_token_env_var: str = "CODE_REVIEW_GITHUB_TOKEN"
    repo_root: str = ""

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
    exclusions = exclusions_section.get("patterns", [])
    github_repo = github_section.get("repo", "")
    output_file = output_section.get("file", "review_output.md")
    overview_model = models_section.get("overview_model", "")
    github_token_env_var = github_section.get("token_env_var", "CODE_REVIEW_GITHUB_TOKEN")

    repo_root = args.repo_path or os.getcwd()

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
        github_token_env_var=github_token_env_var,
        repo_root=repo_root,
    )
