"""The agent's models live in review_config.toml, a committed file (PMAT-312): a model change is a PR, so the eval
gate sees it. .env keeps only the URL and the token. No real .env is ever read."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from review_pull_request import config as config_module
from review_pull_request.config import configured_model, load_config

AGENT_DIR = Path(__file__).resolve().parents[1]


def _args(config, model=None):
    return SimpleNamespace(config=str(config), model=model, base=None, repo_path=None, dry_run=False, verbose=False,
                           hide_pr_comment=False)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for var in ("CODE_REVIEW_URL", "CODE_REVIEW_TOKEN", "CODE_REVIEW_MODEL"):
        monkeypatch.delenv(var, raising=False)
    env = tmp_path / ".env"
    env.write_text("CODE_REVIEW_URL=http://x\nCODE_REVIEW_TOKEN=t\n")
    monkeypatch.setattr(config_module, "ENV_FILE", env)
    toml = tmp_path / "review_config.toml"
    toml.write_text('[model]\nreview = "free/review-model"\n')
    return env, toml


def test_the_review_model_comes_from_review_config_toml(setup):
    _, toml = setup
    assert load_config(_args(toml)).model == "free/review-model"


def test_the_model_flag_still_overrides_it(setup):
    _, toml = setup
    assert load_config(_args(toml, model="flag/model")).model == "flag/model"


def test_a_model_left_in_env_is_ignored_with_a_warning(setup, capsys):
    env, toml = setup
    env.write_text(env.read_text() + "CODE_REVIEW_MODEL=old/env-model\n")
    assert load_config(_args(toml)).model == "free/review-model"
    err = capsys.readouterr().err
    assert "CODE_REVIEW_MODEL" in err and "no longer read" in err and "review_config.toml" in err


def test_a_config_without_the_review_model_exits_naming_the_setting(setup, capsys):
    _, toml = setup
    toml.write_text('[model]\nother = "x/y"\n')
    with pytest.raises(SystemExit):
        load_config(_args(toml))
    assert "[model] review" in capsys.readouterr().err


def test_run_eval_sh_defaults_to_the_review_model():
    """The eval measures the model the reviews use. run_eval.sh reads it with this exact call; run it the same way."""
    script = (AGENT_DIR / "eval" / "scripts" / "run_eval.sh").read_text()
    call = "from review_pull_request.config import configured_model; print(configured_model(\"review\"))"
    assert f"-c '{call}'" in script
    out = subprocess.run([sys.executable, "-c", call], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == configured_model("review") and not out.stderr


def test_the_committed_baseline_is_on_the_reviews_own_model():
    """The gate refuses a run on another model than the baseline's: the two committed files must agree, and there is
    no separate gate model (owner, PMAT-312: gating on one model and reviewing with another defeats the evals)."""
    baseline = json.loads((AGENT_DIR / "eval" / "baseline.json").read_text())
    assert baseline["model"] == configured_model("review")
    import toml
    assert set(toml.load(str(AGENT_DIR / "review_config.toml"))["model"]) == {"review"}
