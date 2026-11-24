"""Acceptance tests for the judgment cache (PMAT-331). Letters are the ticket's acceptance test.

Each runs the real judge: judge_results -> ReviewSession -> ChatOpenAI -> the judge's transports, with a counting
fake endpoint where the network would be. GitHub is faked as in test_judge.py, and the cache is a temporary folder.
"""

import json
import os
import time
from unittest.mock import patch

import httpx
import pytest

import judge_cache
import judge_report
import llm_rubric
from review_pull_request.config import Config, SENSITIVE_PATTERNS
from test_judge import DIFF, FileReader, case, finding, reply, review

CANARY = "CANARY-PMAT-331-a7f3"


def sse(content: str) -> bytes:
    chunks = [{"model": "z-ai/reported", "provider": "Wafer",
               "choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": "stop"}]},
              {"model": "z-ai/reported", "provider": "Wafer", "choices": [],
               "usage": {"prompt_tokens": 100, "completion_tokens": 20, "cost": 0.01}}]
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


class Network(httpx.SyncByteStream):
    """A reply streamed as from the network: httpx reads a ByteStream reply at once, past the recording transport."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __iter__(self):
        yield self._body


@pytest.fixture(autouse=True)
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(judge_cache, "DIR", tmp_path / "judge-cache")
    monkeypatch.delenv("EVAL_JUDGE_CACHE", raising=False)
    return tmp_path / "judge-cache"


@pytest.fixture
def endpoint():
    """The judge model's endpoint: answers each request with the next of `replies` (a dict, text, or a status)."""
    class Endpoint:
        def __init__(self):
            self.requests, self.replies = [], []

        def handle(self, request):
            self.requests.append(request.read().decode())
            answer = self.replies.pop(0) if self.replies else reply()
            if isinstance(answer, int):
                return httpx.Response(answer, json={"error": {"message": "Insufficient credits", "code": answer}})
            text = answer if isinstance(answer, str) else json.dumps(answer)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Network(sse(text)))
    return Endpoint()


@pytest.fixture
def judge(endpoint, tmp_path):
    """judge(model=..., rubric=..., loop_py=...) judges one row through the real chat model; returns the result."""
    rubric = tmp_path / "rubric.md"

    def run(model="z-ai/judge", rubric_text="the rubric", loop_py="def start(key):\n    open_tab(key)\n",
            row=None):
        rubric.write_text(rubric_text)

        class Reader(FileReader):
            def read_file(self, path, max_lines):
                return loop_py if path == "src/loop.py" else super().read_file(path, max_lines)

        cfg = Config(model=model, api_url="http://judge.test", api_token="judge-token",
                     exclusions=list(SENSITIVE_PATTERNS))
        with patch.object(llm_rubric, "judge_config", return_value=cfg), \
                patch.object(llm_rubric, "RUBRIC", rubric), \
                patch.object(llm_rubric, "get_diff", return_value=DIFF), \
                patch.object(llm_rubric, "GitHubFileReader", Reader), \
                patch.object(llm_rubric, "github_token", return_value="gh-token"), \
                patch.object(llm_rubric, "http_transport", lambda url: httpx.MockTransport(endpoint.handle)):
            return llm_rubric.judge_results(row or review(finding()), {"vars": {"case": case(min_severity="low")}})
    return run


def judgment(result):
    return result["metadata"]["judgment"]


def test_a_the_same_row_twice_makes_one_model_call_and_both_judgments_are_equal(judge, endpoint):
    first, second = judgment(judge()), judgment(judge())
    assert len(endpoint.requests) == 1
    for d in llm_rubric.DIMENSIONS:
        assert first[d] == second[d]
    assert "judge_cache" not in first and second["judge_cache"] == "hit"
    assert second["model_reported"] == "z-ai/reported" and second["provider"] == "Wafer"
    assert "cost" not in second and "prompt_tokens" not in second, "a hit spends nothing"
    assert judge_cache.KEY not in first and judge_cache.KEY not in second


@pytest.mark.parametrize("change", [{"rubric_text": "the rubric."}, {"model": "z-ai/judge-2"},
                                    {"loop_py": "def start(key):\n    open_tab(keys)\n"}],
                         ids=["rubric text", "judge model", "one character of the prompt"])
def test_b_any_change_to_the_request_makes_a_new_call(judge, endpoint, change):
    judge()
    judge(**change)
    assert len(endpoint.requests) == 2
    assert endpoint.requests[0] != endpoint.requests[1], "the change reached the request"
    judge(**change)
    assert len(endpoint.requests) == 2, "and the changed request is then cached under its own key"


def test_c_a_402_is_not_cached_the_next_attempt_calls_the_model(judge, endpoint, cache_dir):
    endpoint.replies = [402]
    assert "judge_error" in judge()["metadata"]
    assert not list(cache_dir.glob("*.json"))
    assert "judgment" in judge()["metadata"]
    assert len(endpoint.requests) == 2


def test_c_a_bad_reply_is_not_cached_the_next_attempt_calls_the_model(judge, endpoint):
    endpoint.replies = ["not json"]
    assert "judge_error" in judge()["metadata"]
    assert "judgment" in judge()["metadata"]
    assert len(endpoint.requests) == 2


def test_d_the_store_holds_no_prompt_text(judge, endpoint, cache_dir):
    judge(loop_py=f"def start(key):\n    open_tab(key)  # {CANARY}\n")
    assert CANARY in endpoint.requests[0], "the canary reached the model, so the test can fail"
    stored = [p for p in cache_dir.rglob("*") if p.is_file()]
    assert stored, "nothing was stored, so the test proves nothing"
    for path in stored:
        assert CANARY not in path.read_text()
    entry = json.loads(stored[0].read_text())
    assert set(entry) <= {"judgment", "date", *judge_cache.STORED}


def test_e_off_calls_the_model_even_when_the_cache_has_the_row_and_stores_nothing(judge, endpoint, cache_dir,
                                                                                  monkeypatch):
    judge()
    before = {p: p.read_bytes() for p in cache_dir.iterdir()}
    monkeypatch.setenv("EVAL_JUDGE_CACHE", "off")
    assert "judge_cache" not in judgment(judge())
    assert len(endpoint.requests) == 2
    assert {p: p.read_bytes() for p in cache_dir.iterdir()} == before


def test_an_entry_unused_for_30_days_is_pruned_and_a_hit_restarts_its_days(judge, endpoint, cache_dir):
    judge()
    (entry,) = cache_dir.glob("*.json")
    old = cache_dir / ("0" * 64 + ".json")
    old.write_text(entry.read_text())
    day = 86400
    os.utime(old, (time.time() - 31 * day,) * 2)
    os.utime(entry, (time.time() - 29 * day,) * 2)
    judge()  # a hit
    judge_cache.prune(now=time.time() + 2 * day)
    assert entry.exists() and not old.exists()
    assert len(endpoint.requests) == 1


def test_the_run_prunes_the_cache_at_its_start(cache_dir, monkeypatch):
    cache_dir.mkdir()
    old = cache_dir / ("0" * 64 + ".json")
    old.write_text("{}")
    os.utime(old, (time.time() - 31 * 86400,) * 2)
    llm_rubric.startup_check()
    assert not old.exists()


def test_a_torn_entry_is_a_miss(judge, endpoint, cache_dir):
    judge()
    (entry,) = cache_dir.glob("*.json")
    entry.write_text('{"judg')
    assert "judge_cache" not in judgment(judge())
    assert len(endpoint.requests) == 2


def test_the_silver_labeller_s_chat_model_does_not_cache():
    """make_judge_chat_model is shared with PMAT-326's labeller, which never asks for the cache."""
    cfg = Config(model="m", api_url="http://judge.test", api_token="t")
    client = llm_rubric.make_judge_chat_model({})(cfg).http_client
    assert not isinstance(client._transport._inner, judge_cache.CachingTransport)


def test_the_report_counts_rows_from_the_cache_at_no_cost():
    def row(**meta):
        return {"case": case(), "repeat": 0, "error": None, "skipped": None, "line_pass": True,
                "judgment": {**{d: {"score": 4, "why": "w"} for d in llm_rubric.DIMENSIONS}, "rubric": "v1",
                             "model_requested": "z-ai/judge", "model_reported": "z-ai/reported", **meta}}
    s = judge_report.summarise([row(cost=0.01, prompt_tokens=100, completion_tokens=20), row(judge_cache="hit")])
    assert s["from_cache"] == 1
    assert s["cost"] == 0.01 and s["tokens"] == 120
    assert s["models"] == {"z-ai/reported": 2}, "a cached judgment still names the model that made it"
