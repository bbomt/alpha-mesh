"""Acceptance tests for the Promptfoo harness (PMAT-304 (a), (b)).

The provider runs the real review core against a recording fake GitHub (tests.test_golden_master)
and a scripted model, the same boundaries test_review_core fakes. Nothing reaches GitHub or a model.
"""

import json
import multiprocessing
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import jsonschema
import pytest

import agent_provider
import asserts
import case_rules
import load_cases
from review_pull_request.config import Config, SENSITIVE_PATTERNS
from review_pull_request.llm import LLMError
from review_pull_request.stage2 import run_file_review
from review_pull_request.trace import load as load_trace
from tests import test_golden_master as gm
from tests.helpers import chat_model_factory
from tests.test_canary import _store_text

X = "5" * 40  # raised_sha: a commit before the PR's head (gm.HEAD)


def case(action="raise_true_bug", flag_file="src/cart.py", line=1, **expected):
    c = {"case_id": f"R7-{action}", "repo": f"{gm.OWNER}/{gm.REPO}", "pr_number": gm.PR, "base_sha": gm.BASE,
         "head_sha": gm.HEAD, "raised_sha": X, "fix_sha": None, "label": "real_issue", "category": "logic",
         "notes": "", "expected": {"action": action, "flag_file": flag_file, **expected}}
    if action != "raise_nothing":
        c["finding"] = {"file": flag_file, "line": line, "severity": "medium", "summary": "s"}
    return c


TRUE_BUG = case(min_severity="medium")


def model_reporting(issues_by_file):
    """Overview text; per file, the issues given for it (none by default)."""
    def model(messages, tools):
        if tools is None:
            return SimpleNamespace(content="Overview.", tool_calls=[])
        user = next(m["content"] for m in messages if m["role"] == "user")
        target = next(p for p in gm.FILES if p in user)
        return SimpleNamespace(content=json.dumps({"summary": f"Reviewed {target}.",
                                                   "issues": issues_by_file.get(target, [])}), tool_calls=[])
    return model


@pytest.fixture
def run(tmp_path, monkeypatch):
    """call_api for one case through the fakes; returns (provider result, fake GitHub)."""
    monkeypatch.setitem(gm.DIFFS, f"{gm.BASE}...{X}", gm.DIFFS[f"{gm.BASE}...{gm.HEAD}"])
    monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.setenv("HOME", str(tmp_path))  # the core's run locks go to ~/.mesh/locks

    def call(c, model, repeat=0):
        github = gm.FakeGitHub()
        with fakes(github, model):
            result = call_api(c, repeat)
        return result, github

    def fakes(github, model):
        cfg = Config(model="m", api_url="http://x", api_token="t", exclusions=list(SENSITIVE_PATTERNS))
        with ExitStack() as stack:  # a patch that fails to start undoes the ones before it
            for p in (patch("agent_provider.load_config", return_value=cfg),
                      patch("review_pull_request.session.make_chat_model", chat_model_factory(model)),
                      patch("subprocess.run", github.run),
                      patch.object(httpx.HTTPTransport, "handle_request", lambda self, req: github.handle(req))):
                stack.enter_context(p)
            return stack.pop_all()

    def call_api(c, repeat):
        return agent_provider.call_api(c["case_id"], {"config": {}},
                                       {"vars": {"case_id": c["case_id"], "case": c}, "repeatIndex": repeat})

    def call_together(calls, model):
        """call_api for each (case, repeat) at once, each in its own forked process as Promptfoo's workers
        are (the core sets signal handlers, so not threads); returns the results and every diff fetched."""
        ctx = multiprocessing.get_context("fork")
        queue = ctx.Queue()

        def worker(i, c, repeat):
            github = gm.FakeGitHub()
            with fakes(github, model):
                result = call_api(c, repeat)
            queue.put((i, result, github.diff_ranges))

        procs = [ctx.Process(target=worker, args=(i, c, repeat)) for i, (c, repeat) in enumerate(calls)]
        try:
            for proc in procs:
                proc.start()
            got = sorted(queue.get(timeout=60) for _ in procs)
        finally:  # a child that hangs or fails is never left running after the test
            for proc in procs:
                if proc.is_alive():
                    proc.join(5)
                if proc.is_alive():
                    proc.kill()
                    proc.join()
        return [r for _, r, _ in got], [d for _, _, ranges in got for d in ranges]
    call.together = call_together
    return call


def grade(c, output):
    context = {"vars": {"case": c}}
    return asserts.schema_valid(output, context), asserts.case_expectations(output, context)


# (a) ────────────────────────────────────────────────────────────────────────────────────────────

def test_a_case_is_reviewed_at_raised_sha_never_at_the_prs_head(run):
    result, github = run(TRUE_BUG, model_reporting({}))

    assert "error" not in result, result
    assert github.diff_ranges == [f"{gm.BASE}...{X}"]
    assert github.reads == [] or all(ref == X for _, ref in github.reads)
    assert github.trees == [X]
    assert github.posted == [] and github.unexpected == []
    assert result["output"]["head_sha"] == X and result["output"]["files"] == ["src/cart.py", "src/util.py"]


def test_cases_on_one_commit_share_one_review_within_a_repeat_but_not_across_repeats(run):
    first, github = run(TRUE_BUG, model_reporting({}))
    same_commit, again = run(case("skip_false_bug", "src/util.py"), model_reporting({}))
    next_repeat, fresh = run(TRUE_BUG, model_reporting({}), repeat=1)

    assert again.diff_ranges == [] and same_commit["output"]["findings"] == first["output"]["findings"]
    assert fresh.diff_ranges == [f"{gm.BASE}...{X}"]


def model_waiting_for(parties):
    """model_reporting({}), whose overview call waits until `parties` reviews have reached it: a review
    that waits for another one to finish first breaks the barrier (BrokenBarrierError) instead."""
    barrier, report = multiprocessing.get_context("fork").Barrier(parties, timeout=10), model_reporting({})

    def model(messages, tools):
        if tools is None:
            barrier.wait()
        return report(messages, tools)
    return model


def test_the_repeats_of_one_case_review_at_the_same_time_each_into_its_own_store_and_trace(run, tmp_path):
    (r0, r1), diffs = run.together([(TRUE_BUG, 0), (TRUE_BUG, 1)], model_waiting_for(2))

    assert "error" not in r0 and "error" not in r1, (r0, r1)
    assert diffs == [f"{gm.BASE}...{X}"] * 2  # two reviews, neither reused the other's
    assert r0["output"]["files"] == r1["output"]["files"] == ["src/cart.py", "src/util.py"]
    for n, r in enumerate((r0, r1)):
        trace = Path(r["output"]["trace"])
        assert trace.parent == tmp_path / "run" / "traces" / f"repeat-{n}" and trace.is_file()
        assert "src/cart.py" in _store_text(tmp_path / "run" / f"checkpoints-repeat-{n}.sqlite")


def test_cases_on_one_commit_at_the_same_time_in_one_repeat_still_share_one_review(run):
    (first, second), diffs = run.together([(TRUE_BUG, 0), (case("skip_false_bug", "src/util.py"), 0)],
                                           model_reporting({}))

    assert "error" not in first and "error" not in second, (first, second)
    assert diffs == [f"{gm.BASE}...{X}"]  # one review; the other case reused it
    assert first["output"]["findings"] == second["output"]["findings"]


# (b) ────────────────────────────────────────────────────────────────────────────────────────────

def issue(line, severity="medium", comment="Index can go out of range.", file="src/cart.py"):
    return {"file": file, "line": line, "severity": severity, "comment": comment, "suggestion": None}


def test_a_true_bug_raised_near_its_line_passes(run):
    result, _ = run(TRUE_BUG, model_reporting({"src/cart.py": [issue(3)]}))  # line 1 ± 3

    valid, expected = grade(TRUE_BUG, result["output"])
    assert valid["pass"], valid["reason"]
    assert expected["pass"], expected["reason"]


def test_a_true_bug_that_is_missing_fails_and_says_why(run):
    result, _ = run(TRUE_BUG, model_reporting({"src/cart.py": [issue(9)], "src/util.py": [issue(1, file="src/util.py")]}))

    _, expected = grade(TRUE_BUG, result["output"])
    assert not expected["pass"]
    assert expected["reason"] == ("raise_true_bug: no finding in src/cart.py within ±3 lines of line 1 "
                                  "at medium or above")


@pytest.mark.parametrize("c, findings, passes, reason", [
    (case(min_severity="high"), [issue(1, "medium")], False,
     "raise_true_bug: no finding in src/cart.py within ±3 lines of line 1 at high or above"),
    (case(min_severity="medium", must_mention_any=["overflow", "RANGE"]), [issue(1)], True, None),
    (case(min_severity="medium", must_mention_any=["overflow"]), [issue(1)], False,
     "raise_true_bug: no finding in src/cart.py within ±3 lines of line 1 at medium or above "
     "mentioning any of ['overflow']"),
    (case("skip_false_bug", line=20), [issue(1), issue(30)], True, None),  # other findings in the file are fine
    (case("skip_false_bug", line=20), [issue(1), issue(23, comment="Re-raised.")], False,
     "skip_false_bug: re-raised in src/cart.py at line 23 (within ±3 lines of line 20): Re-raised."),
    (case("raise_nothing", "src/**/*.py", min_severity="medium"), [issue(1, "low")], True, None),
    (case("raise_nothing", "src/**/*.py"), [issue(1, "low")], False,
     "raise_nothing: finding in src/cart.py at line 1 (low), which matches src/**/*.py"),
    (case("raise_nothing", "lib/*.py"), [issue(1, "high")], True, None),
    (case("raise_nothing", "src/**", min_severity="medium", max_comments=2), [issue(1, "low"), issue(2, "low")], True, None),
    (case("raise_nothing", "src/**", min_severity="medium", max_comments=2),
     [issue(1, "low"), issue(2, "low"), issue(3, "low")], False,
     "raise_nothing: 3 findings match src/**, more than the 2 allowed"),
    (case("raise_nothing", "src/**", min_severity="medium", max_comments=2), [issue(1, "medium")], False,
     "raise_nothing: finding in src/cart.py at line 1 (medium), which matches src/**"),
    (case("raise_nothing", "lib/*.py", max_comments=0), [issue(1, "low")], True, None),  # only findings in flag_file count
])
def test_each_action_passes_or_fails_with_a_reason(c, findings, passes, reason):
    review = {"head_sha": X, "model": "m", "files": ["src/cart.py"], "incomplete": [], "excluded": [],
              "findings": [dict(f, summary=f.pop("comment")) for f in findings]}
    result = asserts.case_expectations(review, {"vars": {"case": c}})
    assert result["pass"] is passes
    if reason:
        assert result["reason"] == reason


def test_a_case_with_no_pr_is_reviewed_with_its_own_description_and_never_calls_the_pr_api(run):
    c = {k: v for k, v in TRUE_BUG.items() if k != "pr_number"} | {"description": "Adds a bulk discount."}
    prompts = []

    def model(messages, tools):
        prompts.append(next(m["content"] for m in messages if m["role"] == "user"))
        return model_reporting({})(messages, tools)

    result, github = run(c, model)

    assert "error" not in result, result
    assert "Adds a bulk discount." in prompts[0]
    assert github.diff_ranges == [f"{gm.BASE}...{X}"] and github.unexpected == []
    assert grade(c, result["output"])[0]["pass"]


def test_a_glob_case_over_a_file_that_was_not_fully_reviewed_is_an_error(run):
    c = case("raise_nothing", "src/**")

    def unusable_for_cart(messages, tools):
        if tools is not None and "src/cart.py" in next(m["content"] for m in messages if m["role"] == "user"):
            return SimpleNamespace(content="not json", tool_calls=[])
        return model_reporting({})(messages, tools)

    result, _ = run(c, unusable_for_cart)

    assert "output" not in result and "src/cart.py" in result["error"]


def test_a_review_that_leaves_no_result_is_an_error_not_a_failure(run):
    def broken(messages, tools):
        raise RuntimeError("model down")

    result, _ = run(TRUE_BUG, broken)

    assert "output" not in result and result["error"]


def test_a_case_whose_file_was_not_fully_reviewed_is_an_error(run):
    def unusable_for_cart(messages, tools):
        if tools is not None and "src/cart.py" in next(m["content"] for m in messages if m["role"] == "user"):
            return SimpleNamespace(content="not json", tool_calls=[])
        return model_reporting({})(messages, tools)

    result, _ = run(TRUE_BUG, unusable_for_cart)

    assert "output" not in result and "src/cart.py" in result["error"]


def test_the_review_output_matches_its_schema_and_the_schema_rejects_a_broken_one():
    good = {"head_sha": X, "model": "m", "files": [], "incomplete": [], "excluded": [],
            "findings": [{"file": "a.py", "line": None, "severity": "low", "summary": "s", "suggestion": None}],
            "reads": []}  # extra fields are allowed (PMAT-302 adds some)
    jsonschema.validate(good, case_rules.REVIEW_SCHEMA)
    assert not asserts.schema_valid({"findings": [{"file": "a.py"}]}, {})["pass"]


def test_the_loader_turns_every_case_into_a_test_and_refuses_an_invalid_one(tmp_path):
    tests = load_cases.create_tests()
    assert len(tests) == len(case_rules.development_cases())
    assert {a["value"] for a in tests[0]["assert"]} == {"file://promptfoo/asserts.py:schema_valid",
                                                        "file://promptfoo/asserts.py:case_expectations"}
    assert tests[0]["vars"]["case"]["case_id"] == tests[0]["description"]

    bad = tmp_path / "repo" / "bad.json"
    bad.parent.mkdir()
    bad.write_text(json.dumps(case() | {"expected": {"action": "skip_false_bug"}}))
    with pytest.raises(ValueError, match="bad.json"):
        load_cases.create_tests(cases_dir=tmp_path)


def test_the_run_script_turns_promptfoos_templating_off():
    """Promptfoo renders a string var as Nunjucks, which can run code. The schema refuses such a case_id;
    this is the second layer (checked by hand against promptfoo 0.125.1, which has no Python API to test it)."""
    script = (case_rules.EVAL / "scripts" / "run_eval.sh").read_text()
    assert "PROMPTFOO_DISABLE_TEMPLATING=true" in script


# PMAT-322: files_read ───────────────────────────────────────────────────────────────────────────

HELPER_CANARY = "HELPER-CONTENT-CANARY-7f3a"


def model_reading(paths_by_file, on_call=None):
    """Overview text; per file, asks for the paths given for it in one go, then answers with no findings."""
    def model(messages, tools):
        if tools is None:
            return SimpleNamespace(content="Overview.", tool_calls=[])
        user = next(m["content"] for m in messages if m["role"] == "user")
        target = next(p for p in ("src/cart.py", "src/util.py") if f"a/{p}" in user)
        if on_call:
            on_call(target)
        if not any(m["role"] == "tool" for m in messages) and paths_by_file.get(target):
            return SimpleNamespace(content=None, tool_calls=[
                SimpleNamespace(id=f"{target}-{i}", name="read_file", arguments={"path": p, "reason": "check"})
                for i, p in enumerate(paths_by_file[target])])
        return SimpleNamespace(content=json.dumps({"summary": "Reviewed.", "issues": []}), tool_calls=[])
    return model


@pytest.fixture
def helper_file(monkeypatch):
    monkeypatch.setitem(gm.FILES, "src/helper.py", f"{HELPER_CANARY} = 1\n")


def test_files_read_holds_only_the_file_whose_content_the_model_saw(run, helper_file):
    result, github = run(TRUE_BUG, model_reading({"src/cart.py": ["src/helper.py", "src/missing.py", ".env"]}))

    assert result["output"]["files_read"] == ["src/helper.py"]
    assert [p for p, _ in github.reads] == ["src/helper.py", "src/missing.py"], "the excluded file must not be fetched"
    assert asserts.schema_valid(result["output"], {"vars": {"case": TRUE_BUG}})["pass"]


def test_files_read_is_one_sorted_list_for_the_review_and_keeps_a_read_served_from_the_cache(run, helper_file):
    result, github = run(TRUE_BUG, model_reading({"src/cart.py": ["src/util.py", "src/helper.py"],
                                                  "src/util.py": ["src/helper.py"]}))

    assert result["output"]["files_read"] == ["src/helper.py", "src/util.py"]
    assert [p for p, _ in github.reads].count("src/helper.py") == 1  # the second review's read was cached


def test_files_read_is_empty_when_the_model_read_nothing(run):
    result, _ = run(TRUE_BUG, model_reading({}))

    assert result["output"]["files_read"] == []


def test_a_review_of_only_excluded_files_has_an_empty_files_read(run, monkeypatch):
    monkeypatch.setitem(gm.DIFFS, f"{gm.BASE}...{X}", gm._file_diff(".env", "TOKEN=x"))

    result, _ = run(case("raise_nothing", ".env"), model_reading({}))

    assert result["output"]["excluded"] == [".env"] and result["output"]["files_read"] == []


def _stored_and_returned_text(tmp_path, result) -> tuple[str, str]:
    return _store_text(tmp_path / "run" / "checkpoints-repeat-0.sqlite"), json.dumps(result)


READS_HELPER = {"src/cart.py": ["src/helper.py"]}


def test_the_store_holds_no_read_and_the_results_hold_the_path_never_the_files_content(run, helper_file, tmp_path):
    result, _ = run(TRUE_BUG, model_reading(READS_HELPER))
    store, returned = _stored_and_returned_text(tmp_path, result)

    assert "src/cart.py" in store and "src/helper.py" in returned, "the scan saw no paths, so it proves nothing"
    assert "files_read" not in store and "src/helper.py" not in store  # PMAT-325: the trace holds the reads
    assert HELPER_CANARY not in store
    assert HELPER_CANARY not in returned


def test_the_content_scan_finds_content_returned_as_files_read(run, helper_file, tmp_path):
    def leaky(path):
        spans = load_trace(path)
        spans["src/cart.py"]["span"]["output"]["files_read"] = [gm.FILES["src/helper.py"]]
        return spans

    with patch("eval_adapter.load_trace", leaky):
        result, _ = run(TRUE_BUG, model_reading(READS_HELPER))
    _, returned = _stored_and_returned_text(tmp_path, result)

    assert HELPER_CANARY in returned


def test_the_output_names_the_trace_which_holds_what_the_model_was_shown(run, helper_file, tmp_path):
    result, _ = run(TRUE_BUG, model_reading(READS_HELPER))

    path = Path(result["output"]["trace"])
    assert path.parent == tmp_path / "run" / "traces" / "repeat-0"
    tools = load_trace(path)["src/cart.py"]["tools"]
    assert [t["input"]["path"] for t in tools] == ["src/helper.py"] and HELPER_CANARY in tools[0]["output"]


def test_a_review_of_only_excluded_files_has_no_trace(run, monkeypatch):
    monkeypatch.setitem(gm.DIFFS, f"{gm.BASE}...{X}", gm._file_diff(".env", "TOKEN=x"))

    result, _ = run(case("raise_nothing", ".env"), model_reading({}))

    assert result["output"]["trace"] is None
    assert asserts.schema_valid(result["output"], {"vars": {"case": case("raise_nothing", ".env")}})["pass"]


def test_a_review_resumed_after_a_file_reviewed_without_a_trace_leaves_files_read_out(run, helper_file):
    def fail_on_util(chunk, *args):
        if chunk.file_path == "src/util.py":
            raise LLMError("stopped part way")
        return run_file_review(chunk, *args)

    with patch("review_pull_request.trace.Trace.open", lambda self, *a, **k: None), \
            patch("review_pull_request.graph.run_file_review", fail_on_util):  # as before PMAT-325
        stopped, _ = run(TRUE_BUG, model_reading(READS_HELPER))
    assert "left no result" in stopped["error"]

    reviewed = []
    resumed, _ = run(TRUE_BUG, model_reading({"src/util.py": ["src/helper.py"]}, on_call=reviewed.append))

    assert "error" not in resumed, resumed
    assert set(reviewed) == {"src/util.py"}, "the file saved before the change must not be reviewed again"
    assert resumed["output"]["files"] == ["src/cart.py", "src/util.py"]
    # [] would say the agent read nothing, and the judge counts claims about unread code as false.
    assert "files_read" not in resumed["output"]
    assert asserts.schema_valid(resumed["output"], {"vars": {"case": TRUE_BUG}})["pass"]


def test_the_schema_refuses_a_files_read_that_is_not_a_list_of_paths():
    good = {"head_sha": X, "model": "m", "files": [], "incomplete": [], "excluded": [], "findings": []}
    jsonschema.validate({**good, "files_read": ["a.py"]}, case_rules.REVIEW_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**good, "files_read": [{"path": "a.py", "content": "x"}]}, case_rules.REVIEW_SCHEMA)


def test_the_trace_path_survives_the_run_folder_being_a_symlink(run, tmp_path, monkeypatch):
    """A long run's folder is a symlink to ~/.mesh/eval-runs/<run> (outside the worktree, which is deleted
    after merge): the output must name the real file, not the path through the link."""
    real = tmp_path / "mesh-eval-runs" / "run"
    real.mkdir(parents=True)
    (tmp_path / "worktree").mkdir()
    (tmp_path / "worktree" / "run").symlink_to(real)
    monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path / "worktree" / "run"))

    result, _ = run(TRUE_BUG, model_reading({}))

    assert Path(result["output"]["trace"]).parent == real / "traces" / "repeat-0"
