"""Tests for stage 2 (per-file review): model replies, read_file tool use, caching, results."""

from types import SimpleNamespace

import pytest

from review_pull_request.chunker import FileChunk
from review_pull_request.stage2 import run_file_review
from review_pull_request.readers import ReadError
from tests.helpers import FakeLLM, StubReader, make_session


class MockLLMResponse:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class MockConfig:
    model = "dry-run-model"
    api_url = "https://example.com/api"
    api_token = "test-token"
    base_branch = "master"
    max_tool_calls = 10
    max_file_lines = 8000
    exclusions = []
    output_file = "review_output.md"
    github_repo = ""
    github_token_env_var = "GITHUB_TOKEN"
    repo_root = "/tmp"


def _chunk(path="src/a.py"):
    return FileChunk(file_path=path, diff_text=f"diff --git a/{path} b/{path}\n+x\n")


def _tool_call(call_id, path):
    return SimpleNamespace(id=call_id, name="read_file", arguments={"path": path, "reason": "check"})


def _scripted(*tool_call_rounds):
    """A model that makes one round of tool calls per entry, then returns a final JSON answer."""
    rounds = list(tool_call_rounds)

    def respond(messages, tools):
        if rounds:
            return MockLLMResponse(tool_calls=rounds.pop(0))
        return MockLLMResponse(content='{"summary": "ok", "issues": []}')

    return respond


def _review(chunk=None, overview="Overview", config=None, model=None, reader=None):
    """Run one file review through a session whose model and reader are fakes.

    model: a FakeLLM, a callable(messages, tools) -> response, a response, or an Exception.
    """
    config = config or MockConfig()
    if isinstance(model, FakeLLM):
        llm = model
    elif isinstance(model, Exception):
        def raiser(messages, tools):
            raise model
        llm = FakeLLM(raiser)
    elif callable(model):
        llm = FakeLLM(model)
    else:
        llm = FakeLLM(lambda messages, tools: model)
    session = make_session(config, llm=llm, reader=reader or StubReader(default="FILE BODY"))
    return run_file_review(chunk or _chunk(), overview, session)


def _tool_messages(llm):
    """Tool-result contents the model saw on its last call."""
    return [m["content"] for m in llm.calls[-1][0] if m.get("role") == "tool"]


def test_run_file_review_parses_valid_json():
    chunk = FileChunk(
        file_path="src/main.py",
        diff_text="diff --git a/src/main.py b/src/main.py\n--- a/src/main.py\n+++ b/src/main.py\n@@ -1 +1,2 @@\n+line one\n+line two\n",
    )
    reply = MockLLMResponse(content='{"summary": "Adds new helper function.", "issues": []}')

    result = _review(chunk, model=reply)

    assert result.file_path == "src/main.py"
    assert "helper function" in result.summary
    assert result.tool_calls_made == 0


def test_run_file_review_extracts_issues():
    chunk = FileChunk(file_path="src/auth.py", diff_text="diff --git a/src/auth.py b/src/auth.py\n+new line\n")
    reply = MockLLMResponse(
        content='{"summary": "Token validation.", "issues": [{"file": "src/auth.py", "line": 42, "severity": "high", "comment": "Timing issue", "suggestion": "Use hmac.compare_digest."}]}'
    )

    result = _review(chunk, model=reply)

    assert len(result.issues) == 1
    assert result.issues[0].severity == "high"
    assert result.issues[0].comment == "Timing issue"


def test_run_file_review_retry_on_invalid_json():
    """On first invalid JSON we send a retry message; the second answer is used."""
    calls = [0]

    def respond(messages, tools):
        calls[0] += 1
        if calls[0] == 1:
            return MockLLMResponse(content="This is not JSON")
        return MockLLMResponse(content='{"summary": "Recovered.", "issues": []}')

    result = _review(model=respond)

    assert result.summary == "Recovered."
    assert calls[0] == 2


def test_run_file_review_handles_tool_calls():
    calls = [0]

    def respond(messages, tools):
        calls[0] += 1
        if calls[0] == 1:
            return MockLLMResponse(tool_calls=[_tool_call("call_001", "src/main.py")])
        return MockLLMResponse(content='{"summary": "Reviewed.", "issues": []}')

    result = _review(model=respond, reader=StubReader(default="def main():\n    pass\n"))

    assert result.tool_calls_made == 1
    assert result.summary == "Reviewed."


def test_run_file_review_reads_through_the_sessions_reader():
    reader = StubReader(default="remote content")

    _review(model=_scripted([_tool_call("call_github", "src/main.py")]), reader=reader)

    assert reader.asked == ["src/main.py"]


def test_run_file_review_raises_on_llm_error():
    """A failed LLM call must propagate, not return an empty result."""
    from review_pull_request.llm import LLMError

    with pytest.raises(LLMError):
        _review(model=LLMError("API returned 404"))


# --- read_file caching ---

def test_repeated_read_in_one_review_hits_the_file_once_and_tells_the_model():
    llm = FakeLLM(_scripted([_tool_call("1", "pyproject.toml")], [_tool_call("2", "pyproject.toml")]))
    reader = StubReader(default="FILE BODY")

    result = _review(model=llm, reader=reader)

    assert reader.asked == ["pyproject.toml"]
    tool_msgs = _tool_messages(llm)
    assert tool_msgs[0] == "FILE BODY"
    assert "already" in tool_msgs[1].lower()
    assert result.summary == "ok"


def test_read_cache_is_shared_across_file_reviews_in_one_run():
    config = MockConfig()
    config.read_cache = {}
    first_reader, second_reader = StubReader(default="FILE BODY"), StubReader(default="FILE BODY")

    _review(_chunk("a.py"), config=config, model=_scripted([_tool_call("1", "pyproject.toml")]), reader=first_reader)
    _review(_chunk("b.py"), config=config, model=_scripted([_tool_call("2", "pyproject.toml")]), reader=second_reader)

    assert first_reader.asked == ["pyproject.toml"]
    assert second_reader.asked == []  # served from the shared cache
    assert config.read_cache == {"pyproject.toml": "FILE BODY"}


def test_error_results_are_not_cached():
    config = MockConfig()
    config.read_cache = {}

    _review(config=config, model=_scripted([_tool_call("1", "missing.py")]), reader=StubReader())  # not found

    assert config.read_cache == {}


def test_file_whose_content_starts_with_error_is_still_cached():
    config = MockConfig()
    config.read_cache = {}
    body = "Error: connection refused\nfix: restart"

    _review(config=config, model=_scripted([_tool_call("1", "TROUBLESHOOTING.md")]),
            reader=StubReader({"TROUBLESHOOTING.md": body}))

    assert config.read_cache == {"TROUBLESHOOTING.md": body}


def test_read_cache_stops_storing_once_the_size_cap_is_reached():
    config = MockConfig()
    config.read_cache = {}
    config.read_cache_max_bytes = 10
    llm = FakeLLM(_scripted([_tool_call("1", "a.py")], [_tool_call("2", "b.py")]))
    reader = StubReader({"a.py": "x" * 8, "b.py": "y" * 8})  # second would take the cache to 16 > 10

    _review(config=config, model=llm, reader=reader)

    assert _tool_messages(llm) == ["x" * 8, "y" * 8]  # both still served to the model
    assert config.read_cache == {"a.py": "x" * 8}     # but only the first was stored


def test_read_file_log_line_names_the_file_under_review(capsys):
    _review(_chunk("src/a.py"), model=_scripted([_tool_call("1", "pyproject.toml")]))

    err = capsys.readouterr().err
    assert "read_file: pyproject.toml" in err
    assert "reviewing src/a.py" in err.split("read_file: pyproject.toml")[1].splitlines()[0]


def test_failed_read_can_be_retried_within_the_same_review():
    llm = FakeLLM(_scripted([_tool_call("1", "pyproject.toml")], [_tool_call("2", "pyproject.toml")]))
    outcomes = iter([ReadError("Error: GitHub API rate limit exceeded."), "FILE BODY"])

    class FlakyReader(StubReader):
        def read_file(self, path, max_lines):
            self.asked.append(path)
            return next(outcomes)

    reader = FlakyReader()
    _review(model=llm, reader=reader)

    assert len(reader.asked) == 2  # the failed read was not treated as "already read"
    assert _tool_messages(llm) == ["Error: GitHub API rate limit exceeded.", "FILE BODY"]


def test_a_repeated_missing_path_gets_a_note_instead_of_a_fresh_read(capsys):
    llm = FakeLLM(_scripted(*[[_tool_call(str(i), "pkg/x.py")] for i in range(3)]))
    reader = StubReader()

    _review(model=llm, reader=reader)

    assert reader.asked == ["pkg/x.py"]  # read once; the repeats never reach the reader
    first, *repeats = _tool_messages(llm)
    assert "not found" in first
    assert all("does not exist" in m and "do not request it again" in m.lower() for m in repeats)
    err = capsys.readouterr().err
    assert "(not found)" in err and err.count("not re-read") == 2


def test_not_found_suggests_the_same_file_name_from_the_repo():
    llm = FakeLLM(_scripted([_tool_call("1", "pkg/x.py")]))

    _review(model=llm, reader=StubReader({"src/pkg/x.py": "x", "src/y.py": "y"}))

    [msg] = _tool_messages(llm)
    assert "not found" in msg and "Files with the same name: src/pkg/x.py" in msg
    assert "src/y.py" not in msg


def test_not_found_lists_the_nearest_existing_folder():
    """PR #9 shape: `src/` dropped from the path. The nearest real folder shows `src/`."""
    llm = FakeLLM(_scripted([_tool_call("1", "agent/pkg/missing.py")]))

    _review(model=llm, reader=StubReader({"agent/src/pkg/a.py": "", "agent/README.md": ""}))

    [msg] = _tool_messages(llm)
    assert "Nearest existing folder `agent/` contains: README.md, src/" in msg


def test_the_prompt_lists_the_folder_of_the_file_under_review():
    """PR #11 shape: the model guessed stages.py; the real stage files sit beside the reviewed file."""
    llm = FakeLLM(_scripted())
    reader = StubReader({"pkg/session.py": "", "pkg/stage1.py": "", "pkg/stage2.py": "", "pkg/sub/x.py": ""})

    _review(_chunk("pkg/session.py"), model=llm, reader=reader)

    prompt = llm.calls[0][0][0]["content"]
    assert "Files in the folder of the file under review (`pkg/`): session.py, stage1.py, stage2.py, sub/" in prompt


def test_an_excluded_file_is_never_suggested_or_listed():
    config = MockConfig()
    config.exclusions = ["**/secrets/*", "**/.env"]
    llm = FakeLLM(_scripted([_tool_call("1", "pkg/keys.py")]))
    reader = StubReader({"secrets/keys.py": "", "pkg/.env": "", "pkg/a.py": ""})

    _review(_chunk("pkg/a.py"), config=config, model=llm, reader=reader)

    [msg] = _tool_messages(llm)
    prompt = llm.calls[0][0][0]["content"]
    assert "Nearest existing folder `pkg/` contains: a.py" in msg
    for text in (msg, prompt):
        assert "secrets" not in text and ".env" not in text


def test_without_a_file_listing_the_review_still_runs(capsys):
    class NoListing(StubReader):
        def list_files(self):
            return None

    llm = FakeLLM(_scripted([_tool_call("1", "pkg/x.py")]))
    result = _review(model=llm, reader=NoListing())

    assert result.summary == "ok"
    assert _tool_messages(llm) == ["Error: file not found: pkg/x.py"]
    assert "file listing unavailable" in capsys.readouterr().err


def test_other_failed_reads_are_printed(capsys):
    _review(model=_scripted([_tool_call("1", "a.py")]),
            reader=StubReader({"a.py": ReadError("Error: GitHub API rate limit exceeded.")}))

    assert "(failed: Error: GitHub API rate limit exceeded.)" in capsys.readouterr().err


def test_read_cache_bytes_counter_tracks_stored_content():
    config = MockConfig()
    config.read_cache = {}
    config.read_cache_bytes = 0

    _review(config=config, model=_scripted([_tool_call("1", "a.py"), _tool_call("2", "b.py")]),
            reader=StubReader({"a.py": "xxx", "b.py": "yyé"}))

    assert config.read_cache_bytes == len("xxx".encode()) + len("yyé".encode())


# --- severity normalisation, incomplete results, JSON extraction ---

def _final(issues_json):
    return MockLLMResponse(content='{"summary": "s", "issues": %s}' % issues_json)


def test_unknown_severities_are_normalised_so_findings_are_not_dropped():
    issues = (
        '[{"file":"a","line":1,"severity":"error","comment":"c1"},'
        '{"file":"a","line":2,"severity":"critical","comment":"c2"},'
        '{"file":"a","line":3,"severity":"warning","comment":"c3"},'
        '{"file":"a","line":4,"severity":"weird","comment":"c4"},'
        '{"file":"a","line":5,"severity":"HIGH","comment":"c5"},'
        '{"file":"a","line":6,"severity":"blocker","comment":"c6"},'
        '{"file":"a","line":7,"severity":"major","comment":"c7"},'
        '{"file":"a","line":8,"severity":"moderate","comment":"c8"},'
        '{"file":"a","line":9,"severity":"minor","comment":"c9"}]'
    )

    result = _review(model=_final(issues))

    # unknown values default to medium (like a missing severity), never silently to low
    assert [i.severity for i in result.issues] == [
        "high", "high", "medium", "medium", "high", "high", "high", "medium", "low"]


def test_unparseable_final_response_marks_the_result_incomplete():
    assert _review(model=MockLLMResponse(content="not json at all")).incomplete is True


def test_at_the_read_limit_the_model_is_asked_to_answer_and_its_findings_are_kept():
    config = MockConfig()
    config.max_tool_calls = 1

    def respond(messages, tools):
        if tools is None:  # read limit reached: no tools offered, an answer is expected
            return _final('[{"file":"a.py","line":1,"severity":"high","comment":"bug"}]')
        return MockLLMResponse(tool_calls=[_tool_call(str(len(messages)), "a.py")])

    llm = FakeLLM(respond)
    result = _review(config=config, model=llm)

    assert result.incomplete is False
    assert [i.comment for i in result.issues] == ["bug"]
    assert llm.calls[-1][1] is None
    assert "no more" in llm.calls[-1][0][-1]["content"].lower()


def test_a_model_that_keeps_asking_to_read_past_the_limit_is_incomplete():
    config = MockConfig()
    config.max_tool_calls = 1

    def respond(messages, tools):
        return MockLLMResponse(tool_calls=[_tool_call(str(len(messages)), "a.py")])

    assert _review(config=config, model=FakeLLM(respond)).incomplete is True


def test_a_normal_result_is_not_incomplete():
    assert _review(model=_final("[]")).incomplete is False


def test_json_is_found_inside_prose_and_fences():
    from review_pull_request.stage2 import _parse_json_response

    body = '{"summary": "ok", "issues": []}'
    assert _parse_json_response("Here is my review:\n```json\n" + body + "\n```\nHope it helps.") == {"summary": "ok", "issues": []}
    assert _parse_json_response("Sure! " + body + " Let me know.") == {"summary": "ok", "issues": []}
    assert _parse_json_response("```json\n" + body + "\n```") == {"summary": "ok", "issues": []}
    assert _parse_json_response("nothing here") is None


def test_the_answer_after_an_echoed_schema_example_wins():
    from review_pull_request.stage2 import _parse_json_response

    echoed = '```json\n{"summary": "example", "issues": [{"severity": "high"}]}\n```'
    answer = '```json\n{"summary": "real", "issues": []}\n```'
    assert _parse_json_response(f"The schema is:\n{echoed}\nMy review:\n{answer}")["summary"] == "real"


def test_model_cannot_read_excluded_files_through_the_read_tool():
    config = MockConfig()
    config.exclusions = ["**/.env"]
    llm = FakeLLM(_scripted([_tool_call("1", ".env")]))
    reader = StubReader({".env": "TOKEN=super-secret-value"})

    _review(config=config, model=llm, reader=reader)

    assert reader.asked == []  # the policy refused before any read
    shown = _tool_messages(llm)
    assert all("super-secret-value" not in c for c in shown)
    assert "excluded" in shown[0]


@pytest.mark.parametrize("payload", [
    '{"summary": "s", "issues": null}',
    '{"summary": "s", "issues": "none"}',
    '{"summary": "s", "issues": ["oops", 3, null]}',
])
def test_malformed_issues_payloads_degrade_instead_of_aborting_the_run(payload):
    result = _review(model=MockLLMResponse(content=payload))

    assert result.issues == []
    assert result.incomplete is True  # the model's answer was unusable, so do not report it as clean


def test_equivalent_path_spellings_share_dedup_and_cache():
    config = MockConfig()
    config.read_cache = {}
    reader = StubReader(default="BODY")
    model = _scripted([_tool_call("1", "src/x.py")], [_tool_call("2", "./src/x.py")], [_tool_call("3", "/src/x.py")])

    _review(config=config, model=model, reader=reader)

    assert len(reader.asked) == 1
    assert list(config.read_cache) == ["src/x.py"]
