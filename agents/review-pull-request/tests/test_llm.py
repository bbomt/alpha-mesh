"""Tests for llm.chat error handling: malformed 200 responses are LLMErrors."""

import json
from unittest.mock import MagicMock, patch

import pytest

from review_pull_request.llm import LLMError, chat


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
