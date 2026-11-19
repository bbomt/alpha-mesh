"""The chat model is built from CODE_REVIEW_URL and the model name (spec 3.7)."""

import pytest

from review_pull_request.config import Config
from review_pull_request.session import make_chat_model


def _model(url, name):
    return make_chat_model(Config(model=name, api_url=url, api_token="secret-token-1"), name)


def test_openrouter_gets_the_full_provider_slash_model_name():
    assert _model("https://openrouter.ai/api", "openai/gpt-oss-120b").model_name == "openai/gpt-oss-120b"


@pytest.mark.parametrize("url", ["https://api.minimax.io", "http://localhost:11434", "https://api.openai.com"])
def test_other_endpoints_get_the_provider_prefix_stripped(url):
    assert _model(url, "minimax/minimax-m2").model_name == "minimax-m2"


def test_a_name_without_a_provider_is_sent_unchanged():
    assert _model("https://api.minimax.io", "MiniMax-M2").model_name == "MiniMax-M2"


@pytest.mark.parametrize("url, base", [
    ("https://openrouter.ai/api", "https://openrouter.ai/api/v1"),
    ("https://openrouter.ai/api/", "https://openrouter.ai/api/v1"),
    ("http://localhost:11434/v1", "http://localhost:11434/v1"),
])
def test_v1_is_appended_to_the_base_url_only_if_missing(url, base):
    assert _model(url, "m").openai_api_base == base


def test_key_comes_from_config_not_provider_env_vars_and_retries_are_ours(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "wrong-key-from-env")
    model = _model("https://api.minimax.io", "m")

    assert model.openai_api_key.get_secret_value() == "secret-token-1"
    assert model.max_retries == 0  # LangChainLLM retries (PMAT-236), the SDK never does
    assert model.streaming is True  # the idle limit needs reply data as it is generated
