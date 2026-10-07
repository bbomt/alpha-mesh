"""Tests for llm.chat error handling: malformed 200 responses are LLMErrors."""

import json
from unittest.mock import MagicMock, patch

import pytest

from review_pull_request.llm import LLMError, chat


@pytest.fixture(autouse=True)
def _no_backoff_sleep():
    with patch("review_pull_request.llm.time.sleep"):
        yield


class _Config:
    model = "m"
    api_url = "http://x"
    api_token = "t"


def _response(json_value=None, json_error=None):
    r = MagicMock(status_code=200, text="body")
    if json_error:
        r.json.side_effect = json_error
    else:
        r.json.return_value = json_value
    return r


def test_non_json_200_body_raises_llm_error():
    resp = _response(json_error=json.JSONDecodeError("bad", "doc", 0))
    with patch("review_pull_request.llm.httpx.post", return_value=resp):
        with pytest.raises(LLMError):
            chat([{"role": "user", "content": "hi"}], None, _Config())


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{}]}])
def test_200_body_without_choices_raises_llm_error(payload):
    with patch("review_pull_request.llm.httpx.post", return_value=_response(payload)):
        with pytest.raises(LLMError):
            chat([{"role": "user", "content": "hi"}], None, _Config())


def test_non_object_tool_call_arguments_raise_llm_error():
    body = {"choices": [{"finish_reason": "tool_calls", "message": {"tool_calls": [
        {"id": "1", "function": {"name": "read_file", "arguments": "[1, 2]"}}]}}]}
    with patch("review_pull_request.llm.httpx.post",
               return_value=_response(body)):
        with pytest.raises(LLMError):
            chat([{"role": "user", "content": "hi"}], None, _Config())


def test_one_malformed_200_body_is_retried_before_giving_up():
    good = _response({"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]})
    bad = _response({})
    with patch("review_pull_request.llm.httpx.post", side_effect=[bad, good]) as post, \
         patch("review_pull_request.llm.time.sleep"):
        result = chat([{"role": "user", "content": "hi"}], None, _Config())

    assert result.content == "ok"
    assert post.call_count == 2
