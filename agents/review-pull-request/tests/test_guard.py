"""Redactor / GuardedLLM: the last line of defence at the model boundary."""

import json

from review_pull_request.guard import GuardedLLM, Redactor
from tests.helpers import FakeLLM

API_TOKEN = "sk-or-v1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_literal_secrets_are_replaced():
    red = Redactor([API_TOKEN])

    out = red.scrub([{"role": "user", "content": f"key is {API_TOKEN} ok"}])

    assert API_TOKEN not in json.dumps(out) and "[REDACTED]" in out[0]["content"]


def test_token_shaped_strings_are_replaced():
    red = Redactor([])
    text = "gh ghp_abcdefghijklmnopqrstuvwxyz0123456789AB aws AKIAABCDEFGHIJKLMNOP sk-abcdefghijklmnopqrstuv"

    out = red.scrub([{"role": "tool", "content": text}])[0]["content"]

    for leaked in ("ghp_abcdef", "AKIAABCDEF", "sk-abcdefghij"):
        assert leaked not in out


def test_ordinary_text_and_non_string_content_are_untouched():
    red = Redactor([API_TOKEN])
    messages = [{"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
                {"role": "user", "content": "def add(a, b): return a + b"}]

    assert red.scrub(messages) == messages


def test_scrubbing_does_not_mutate_the_callers_messages():
    messages = [{"role": "user", "content": f"x {API_TOKEN}"}]

    Redactor([API_TOKEN]).scrub(messages)

    assert API_TOKEN in messages[0]["content"]


def test_guarded_llm_scrubs_before_delegating_and_returns_the_answer():
    inner = FakeLLM(content="answer")
    guarded = GuardedLLM(inner, Redactor([API_TOKEN]))

    response = guarded.chat([{"role": "user", "content": f"token {API_TOKEN}"}], tools=None)

    assert response.content == "answer"
    shown = json.dumps(inner.calls[0][0])
    assert API_TOKEN not in shown


def test_fine_grained_github_tokens_are_replaced():
    token = "github_pat_11ABCDEFG0123456789_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUV"

    out = Redactor([]).scrub([{"role": "tool", "content": f"TOKEN={token}"}])[0]["content"]

    assert "github_pat_11ABC" not in out


def test_a_private_key_body_is_redacted_not_just_its_header():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA1234secretkeybody\n-----END RSA PRIVATE KEY-----"

    out = Redactor([]).scrub([{"role": "user", "content": f"key:\n{pem}\nafter"}])[0]["content"]

    assert "MIIEowIBAAKCAQEA1234secretkeybody" not in out
    assert out.endswith("after")
