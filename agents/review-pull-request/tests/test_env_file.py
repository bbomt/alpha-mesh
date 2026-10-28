"""Tests for loading .env from an explicit path in Python only (PMAT-231). No real .env is ever read."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from review_pull_request import config as config_module
from review_pull_request.config import ENV_FILE, REQUIRED_ENV_VARS, load_config

AGENT_DIR = Path(__file__).resolve().parents[1]


def _args(dry_run=False):
    return SimpleNamespace(config=None, model=None, base=None, repo_path=None,
                           dry_run=dry_run, verbose=False, hide_pr_comment=False)


@pytest.fixture
def env_file(tmp_path, monkeypatch):
    for var in REQUIRED_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    path = tmp_path / ".env"
    monkeypatch.setattr(config_module, "ENV_FILE", path)
    return path


def test_env_file_default_is_alpha_mesh_root():
    # Derived from this test file's location, independently of config.py's own computation.
    repo_root = Path(__file__).resolve().parents[3]
    assert (repo_root / "bin" / "mesh").exists()  # sanity: this really is the alpha-mesh root
    assert ENV_FILE == repo_root / ".env"


def test_values_come_from_the_file_at_the_explicit_path(env_file):
    env_file.write_text("CODE_REVIEW_URL=http://from-file\nCODE_REVIEW_TOKEN=t\nCODE_REVIEW_MODEL=file/model\n")

    cfg = load_config(_args())

    assert (cfg.api_url, cfg.api_token, cfg.model) == ("http://from-file", "t", "file/model")


def test_a_variable_already_in_the_environment_wins_over_the_file(env_file, monkeypatch):
    env_file.write_text("CODE_REVIEW_URL=http://from-file\nCODE_REVIEW_TOKEN=t\nCODE_REVIEW_MODEL=file/model\n")
    monkeypatch.setenv("CODE_REVIEW_MODEL", "env/model")

    assert load_config(_args()).model == "env/model"


def test_missing_file_exits_naming_the_path(env_file, capsys):
    with pytest.raises(SystemExit):
        load_config(_args())

    err = capsys.readouterr().err
    assert str(env_file) in err and "not found" in err


def test_missing_required_variable_exits_naming_the_variable_and_path(env_file, capsys):
    env_file.write_text("CODE_REVIEW_URL=http://from-file\nCODE_REVIEW_MODEL=file/model\n")

    with pytest.raises(SystemExit):
        load_config(_args())

    err = capsys.readouterr().err
    assert "CODE_REVIEW_TOKEN is not set" in err and str(env_file) in err


def test_dry_run_uses_file_values_and_fills_only_the_gaps(env_file):
    env_file.write_text("CODE_REVIEW_MODEL=file/model\n")

    cfg = load_config(_args(dry_run=True))

    assert cfg.model == "file/model"
    assert cfg.api_token == "dry-run-placeholder"


def test_dry_run_without_a_file_uses_placeholders(env_file):
    assert load_config(_args(dry_run=True)).model == "dry-run-placeholder"


def test_bash_wrapper_does_not_touch_env_file():
    wrapper = (AGENT_DIR / "bin" / "review-pull-request").read_text(encoding="utf-8")

    assert ".env" not in wrapper
