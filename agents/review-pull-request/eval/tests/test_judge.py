"""Acceptance tests for the LLM judge (PMAT-311). Letters are the ticket's acceptance test.

The canary test (g) is in tests/test_canary.py. (a) and (f) call the real judge model, so they run
only with JUDGE_LIVE=1 (`JUDGE_LIVE=1 uv run pytest eval/tests/test_judge.py -k live`); everything
else uses a fake model and a fake GitHub.
"""

import json
import os
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import yaml

import agent_provider
import case_rules
import judge_agreement
import judge_report
import judge_sample
import llm_rubric
import load_cases
from review_pull_request.config import Config, SENSITIVE_PATTERNS
from tests.helpers import chat_model_factory

SHA = "a" * 40
CLAIM = "start_queued saves the queue before loop.start, so a failed start loses the ticket."
DIFF = ("diff --git a/src/queue.py b/src/queue.py\n--- a/src/queue.py\n+++ b/src/queue.py\n@@ -1,3 +1,5 @@\n"
        " def start_queued(queue, key):\n+    queue.remove(key)\n+    save_queue(queue)\n     loop.start(key)\n")


def case(action="raise_true_bug", flag_file="src/queue.py", line=3, **expected):
    c = {"case_id": "R7-x-001", "repo": "o/r", "base_sha": "b" * 40, "raised_sha": SHA, "category": "mined",
         "notes": "", "label": "real_issue", "expected": {"action": action, "flag_file": flag_file, **expected}}
    if action != "raise_nothing":
        c["finding"] = {"file": flag_file, "line": line, "severity": "high", "summary": CLAIM}
    return c


def finding(summary=CLAIM, file="src/queue.py", line=3, severity="high", suggestion="Save after start."):
    return {"file": file, "line": line, "severity": severity, "summary": summary, "suggestion": suggestion}


def review(*findings, files_read=("src/loop.py",)):
    return {"findings": list(findings), "model": "nvidia/nemotron", "files": ["src/queue.py"], "incomplete": [],
            "files_read": list(files_read)}


LOOP_PY = "def start(key):\n    write_status(key)\n    open_tab(key)  # LOOP-PY-MARKER\n"


class FileReader:
    """The GitHub file reader at the case's commit."""

    def __init__(self, *a, **k):
        pass

    def read_file(self, path, max_lines):
        from review_pull_request.readers import NotFound

        return LOOP_PY if path == "src/loop.py" else NotFound(f"Error: file not found: {path}")

    def close(self):
        pass


def reply(correctness=4, coverage=4, actionability=4):
    return {"correctness": {"why": "c", "score": correctness}, "coverage": {"why": "v", "score": coverage},
            "actionability": {"why": "a", "score": actionability}}


@pytest.fixture
def judged():
    """judge_results through a fake model; returns (the assert's result, the messages the model saw)."""
    def run(c, r, answer):
        seen = []

        def model(messages, tools):
            seen.append(messages)
            return SimpleNamespace(content=answer if isinstance(answer, str) else json.dumps(answer), tool_calls=[])

        cfg = Config(model="z-ai/judge", api_url="http://x", api_token="judge-token",
                     exclusions=list(SENSITIVE_PATTERNS))
        with patch.object(llm_rubric, "judge_config", return_value=cfg), \
                patch.object(llm_rubric, "get_diff", return_value=DIFF), \
                patch.object(llm_rubric, "GitHubFileReader", FileReader), \
                patch.object(llm_rubric, "github_token", return_value="gh-token"), \
                patch.object(llm_rubric, "make_judge_chat_model", lambda meta: chat_model_factory(model)):
            return llm_rubric.judge_results(r, {"vars": {"case": c}}), seen
    return run


# (b) ── a missing JUDGE_* variable fails fast, before any review ─────────────────────────────────
@pytest.mark.parametrize("var", llm_rubric.REQUIRED)
def test_b_a_missing_variable_stops_the_loader_and_names_it(var, tmp_path, monkeypatch):
    monkeypatch.setattr(llm_rubric.config_module, "ENV_FILE", tmp_path / "no.env")
    for v in llm_rubric.REQUIRED:
        monkeypatch.setenv(v, "x")
    monkeypatch.delenv(var)
    with pytest.raises(llm_rubric.JudgeConfigError, match=rf"{var} is not set. Set it in .*no.env \(see .env.example\)"):
        load_cases.create_tests()


@pytest.mark.skipif(not shutil.which("promptfoo"), reason="needs the promptfoo CLI")
def test_b_the_real_run_stops_before_any_review_when_the_token_is_empty(tmp_path):
    """An empty value counts as unset, and load_dotenv keeps it, so the machine's real .env cannot fill it in."""
    run = "pytest-judge-b"
    (tmp_path / "results.json").write_text('{"results": {"results": []}}')
    (tmp_path / "run.json").write_text('{"model": "nvidia/n", "k": 1}')
    done = subprocess.run([str(case_rules.EVAL / "scripts" / "run_eval.sh"), "--stored", str(tmp_path), "--run", run],
                          capture_output=True, text=True, timeout=300, env={**os.environ, "JUDGE_TOKEN": ""})
    shutil.rmtree(case_rules.EVAL / "runs" / run, ignore_errors=True)
    assert done.returncode != 0
    assert "JUDGE_TOKEN is not set" in done.stdout + done.stderr


def test_a_holdout_run_needs_no_judge_settings(monkeypatch):
    monkeypatch.setenv("EVAL_HOLDOUT", "1")
    for v in llm_rubric.REQUIRED:
        monkeypatch.setenv(v, "")
    assert {t["vars"]["case_id"] for t in load_cases.create_tests()} == case_rules.holdout_ids()


def test_a_holdout_case_never_reaches_the_judge():
    """Rubric iterations must not use the hold-out. The assert runs on every test, so the refusal is in the judge."""
    held = sorted(case_rules.holdout_ids())[0]
    with patch.object(llm_rubric, "judge_review", side_effect=AssertionError("the judge read a hold-out case")):
        graded = llm_rubric.judge_results(review(finding()), {"vars": {"case": case() | {"case_id": held}}})
    assert graded["pass"] is True and "hold-out" in graded["reason"] and "namedScores" not in graded


def test_the_config_shows_every_assert_in_order():
    config = yaml.safe_load((case_rules.EVAL / "promptfooconfig.yaml").read_text())
    assert config["defaultTest"]["assert"] == [
        {"type": "python", "value": "file://promptfoo/asserts.py:schema_valid"},
        {"type": "python", "value": "file://promptfoo/asserts.py:case_expectations"},
        {"type": "python", "value": "file://promptfoo/llm_rubric.py:judge_results"}]
    for a in config["defaultTest"]["assert"]:
        path, function = a["value"].removeprefix("file://").split(":")
        assert f"def {function}(" in (case_rules.EVAL / path).read_text()


# (d) ── a stored review is judged without running the agent ──────────────────────────────────────
def stored_results(tmp_path, rows):
    path = tmp_path / "results.json"
    path.write_text(json.dumps({"results": {"results": rows}}))
    return path


def row(case_id, repeat, output=None, error=None):
    return {"vars": {"case_id": case_id}, "repeatIndex": repeat, "error": error,
            "response": {"output": output} if output is not None else {"error": error}}


def test_d_a_stored_review_is_scored_without_running_the_agent(tmp_path, monkeypatch, judged):
    c = case(min_severity="low")
    first, second = review(finding()), review(finding("Another claim."))
    path = stored_results(tmp_path, [row(c["case_id"], 0, json.dumps(first)), row(c["case_id"], 1, second)])
    monkeypatch.setenv("EVAL_STORED_RESULTS", str(path))

    def tripwire(*a, **k):
        raise AssertionError("the agent ran")

    context = {"vars": {"case_id": c["case_id"], "case": c}, "repeatIndex": 1}
    with patch.object(agent_provider, "review", tripwire), patch.object(agent_provider, "review_case", tripwire):
        result = agent_provider.call_api(c["case_id"], {}, context)

    assert result == {"output": second}, "repeat 1's stored review, unchanged"
    graded, seen = judged(c, result["output"], reply())
    assert graded["namedScores"]["correctness"] == 4
    assert seen, "the judge must have read it"


def test_d_the_judge_fetches_only_the_diff_never_the_agent(judged):
    with patch("review_pull_request.review.run_review_agent", side_effect=AssertionError("the agent ran")):
        graded, _ = judged(case(min_severity="low"), review(finding()), reply())
    assert "judge_error" not in graded["metadata"]


def test_d_a_stored_run_refuses_a_holdout_case_and_a_missing_review(tmp_path, monkeypatch):
    held = sorted(case_rules.holdout_ids())[0]
    path = stored_results(tmp_path, [row(held, 0, review())])
    monkeypatch.setenv("EVAL_STORED_RESULTS", str(path))
    c = case()
    assert "hold-out" in agent_provider.call_api(held, {}, {"vars": {"case_id": held, "case": c | {"case_id": held}}})["error"]
    assert "no stored review" in agent_provider.call_api("x", {}, {"vars": {"case_id": "x", "case": c | {"case_id": "x"}}})["error"]


def test_d_a_stored_error_stays_an_error(tmp_path, monkeypatch):
    c = case()
    monkeypatch.setenv("EVAL_STORED_RESULTS", str(stored_results(tmp_path, [row(c["case_id"], 0, error="it broke")])))
    assert "it broke" in agent_provider.call_api("x", {}, {"vars": {"case_id": c["case_id"], "case": c}})["error"]


# The judge's contract ────────────────────────────────────────────────────────────────────────────
def test_the_judge_grades_against_the_ground_truth_with_the_versioned_rubric_at_temperature_0(judged):
    c = case(min_severity="low") | {"notes": "A note about the case.", "description": "Saves the queue earlier."}
    _, seen = judged(c, review(finding()), reply())
    system, user = seen[0][0]["content"], seen[0][1]["content"]
    assert system == llm_rubric.RUBRIC.read_text()
    assert llm_rubric.RUBRIC.name == "rubric.v1.md"
    for part in (CLAIM, "A note about the case.", '"action": "raise_true_bug"', "Save after start.", "save_queue(queue)",
                 "Saves the queue earlier."):
        assert part in user


def test_the_judge_gets_exactly_the_files_the_agent_read_and_no_tools(judged):
    _, seen = judged(case(min_severity="low"), review(finding()), reply())
    assert "LOOP-PY-MARKER" in seen[0][1]["content"]
    _, seen = judged(case(min_severity="low"), review(finding(), files_read=()), reply())
    assert "LOOP-PY-MARKER" not in seen[0][1]["content"]
    assert "The agent read no files" in seen[0][1]["content"]


def test_a_file_the_judge_cannot_fetch_is_named_not_silently_treated_as_unread(judged):
    _, seen = judged(case(min_severity="low"), review(finding(), files_read=("src/gone.py",)), reply())
    assert "src/gone.py" in seen[0][1]["content"] and "could not be fetched" in seen[0][1]["content"]


def test_a_stored_review_without_files_read_is_an_error_not_an_unfair_score(judged):
    r = review(finding())
    del r["files_read"]
    graded, seen = judged(case(min_severity="low"), r, reply())
    assert graded["pass"] is True and "files_read" in graded["metadata"]["judge_error"]
    assert not seen, "the judge must not score without the agent's context"


def test_a_case_from_a_pr_gets_its_description_from_the_pr(judged):
    with patch.object(llm_rubric, "get_pr_description", return_value="From the PR.") as fetch:
        _, seen = judged(case(min_severity="low") | {"pr_number": 7}, review(finding()), reply())
    assert "From the PR." in seen[0][1]["content"]
    assert fetch.call_args.args[0] == "https://github.com/o/r/pull/7"


def test_the_real_chat_model_runs_at_temperature_0_with_the_judge_settings():
    cfg = Config(model="z-ai/m", api_url="https://openrouter.ai/api", api_token="t")
    chat = llm_rubric.make_judge_chat_model({})(cfg)
    assert chat.temperature == 0 and chat.model_name == "z-ai/m" and chat.max_retries == 0
    assert chat.openai_api_base == "https://openrouter.ai/api/v1"


def test_the_judge_never_gates_a_case(judged):
    """Scores aren't used for any gate until the owner sets thresholds, so even the worst passes."""
    graded, _ = judged(case(min_severity="low"), review(finding()), reply(1, 1, 1))
    assert graded["pass"] is True
    assert graded["namedScores"] == {"correctness": 1, "coverage": 1, "actionability": 1}


@pytest.mark.parametrize("answer, why", [
    ("not json", "JSON"),
    (reply(correctness=6), "correctness"),
    (reply(correctness=True), "correctness"),
    ({**reply(), "coverage": {"why": "v"}}, "coverage"),
])
def test_an_unusable_reply_is_recorded_not_failed(judged, answer, why):
    graded, _ = judged(case(min_severity="low"), review(finding()), answer)
    assert graded["pass"] is True and why in graded["metadata"]["judge_error"]
    assert "namedScores" not in graded


def test_a_fenced_json_reply_is_accepted(judged):
    graded, _ = judged(case(min_severity="low"), review(finding()), f"```json\n{json.dumps(reply())}\n```")
    assert graded["namedScores"]["coverage"] == 4


def test_with_no_findings_only_coverage_is_scored(judged):
    graded, _ = judged(case("raise_nothing", "src/**"), review(), reply(5, 5, 5))
    assert graded["namedScores"] == {"coverage": 5}
    judgment = graded["metadata"]["judgment"]
    assert judgment["correctness"]["score"] is None and judgment["actionability"]["score"] is None


def test_the_family_warning_checks_the_runs_model_and_the_env_model(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(llm_rubric.config_module, "ENV_FILE", tmp_path / "no.env")
    for var, value in {"JUDGE_MODEL": "z-ai/glm", "JUDGE_URL": "u", "JUDGE_TOKEN": "t",
                       "EVAL_MODEL": "nvidia/n", "CODE_REVIEW_MODEL": "nvidia/m"}.items():
        monkeypatch.setenv(var, value)
    llm_rubric.startup_check()
    err = capsys.readouterr().err
    assert "Judge model: z-ai/glm" in err and "WARNING" not in err

    monkeypatch.setenv("CODE_REVIEW_MODEL", "z-ai/other")
    llm_rubric.startup_check()
    assert "WARNING: JUDGE_MODEL z-ai/glm and CODE_REVIEW_MODEL z-ai/other share a family" in capsys.readouterr().err

    monkeypatch.setenv("EVAL_MODEL", "z-ai/third")
    monkeypatch.setenv("CODE_REVIEW_MODEL", "nvidia/m")
    llm_rubric.startup_check()
    assert "WARNING: JUDGE_MODEL z-ai/glm and the run's model z-ai/third share a family" in capsys.readouterr().err


SSE = (b'data: {"model":"z-ai/reported","provider":"Wafer","choices":[{"delta":{"content":"hi"}}]}\n\n'
       b'data: {"model":"z-ai/reported","provider":"Wafer","choices":[],'
       b'"usage":{"prompt_tokens":10,"completion_tokens":2,"cost":0.001}}\n\ndata: [DONE]\n\n')


def test_the_reported_model_provider_and_cost_are_recorded_from_the_stream():
    meta = {}

    class Inner(httpx.BaseTransport):
        def handle_request(self, request):
            return httpx.Response(200, stream=httpx.ByteStream(SSE))

    response = llm_rubric.RecordingTransport(Inner(), meta).handle_request(httpx.Request("POST", "http://x"))
    assert b"".join(response.stream) == SSE, "the reply must pass through unchanged"
    assert meta == {"model_reported": "z-ai/reported", "provider": "Wafer", "prompt_tokens": 10,
                    "completion_tokens": 2, "cost": 0.001}


def test_the_recording_survives_a_reader_that_stops_at_done_and_a_chunk_split_mid_line():
    """The OpenAI client stops reading at [DONE], so a recorder that waits for the stream's end sees nothing."""
    meta = {}
    chunks = [SSE[:40], SSE[40:SSE.index(b"data: [DONE]")], b"data: [DONE]\n\n"]
    stream = llm_rubric._RecordingStream(iter(chunks), meta)
    for chunk in stream:
        if b"[DONE]" in chunk:
            break
    assert meta["provider"] == "Wafer" and meta["cost"] == 0.001


# (e) ── the 25-case sample ───────────────────────────────────────────────────────────────────────
def test_e_the_committed_sample_holds_25_development_cases_and_no_holdout_case():
    sample = json.loads(judge_sample.SAMPLE.read_text())["cases"]
    assert len(sample) == len(set(sample)) == 25
    assert not set(sample) & case_rules.holdout_ids()
    assert set(sample) <= {c["case_id"] for c in case_rules.development_cases()}


def test_e_the_committed_sample_is_the_draw_and_follows_the_agreed_split():
    cases = case_rules.development_cases()
    assert json.loads(judge_sample.SAMPLE.read_text())["cases"] == judge_sample.draw(cases)
    by_id = {c["case_id"]: c for c in cases}
    counts = {}
    for case_id in judge_sample.draw(cases):
        group = judge_sample.group(by_id[case_id])
        counts[group] = counts.get(group, 0) + 1
    assert counts == judge_sample.QUOTA


def test_e_mined_cases_spread_over_commits_as_evenly_as_they_can():
    by_id = {c["case_id"]: c for c in case_rules.development_cases()}
    per_commit = {}
    for case_id in judge_sample.draw(list(by_id.values())):
        c = by_id[case_id]
        if c["category"] == "mined":
            per_commit[c["raised_sha"]] = per_commit.get(c["raised_sha"], 0) + 1
    # 11 cases, 6 commits, and two commits hold only one development case each that can go in: with the other
    # four at 2 each that is 10 cases, so one commit must give 3. Nothing evener exists.
    assert sorted(per_commit.values()) == [1, 1, 2, 2, 2, 3]


def test_e_the_draw_never_reads_results():
    source = (judge_sample.SAMPLE.parent.parent / "scripts" / "judge_sample.py").read_text()
    for word in ("results.json", "baseline", "error_analysis", "gradingResult"):
        assert word not in source


# (c) ── judge–human agreement ────────────────────────────────────────────────────────────────────
def scores(correctness=4, coverage=4, actionability=4):
    return {"correctness": correctness, "coverage": coverage, "actionability": actionability}


def test_c_agreement_within_one_point_per_dimension_leaves_out_nulls():
    human = {"a": scores(5, 2, None), "b": scores(3, 2, 4), "c": scores(1, 5, 4)}
    judge = {"a": scores(4, 4, 4), "b": scores(3, 3, None), "c": scores(3, 5, 5)}
    out = judge_agreement.agreement(human, judge)
    assert out["correctness"]["within_1"] == pytest.approx(2 / 3) and out["correctness"]["n"] == 3
    assert out["coverage"]["within_1"] == pytest.approx(2 / 3)
    assert out["actionability"]["n"] == 1 and out["actionability"]["within_1"] == 1


def test_c_kappa_corrects_for_chance_on_pass_fail():
    # 8 of 10 agree, but both raters pass nearly everything, so kappa is well below 0.8.
    h = [5, 5, 5, 5, 5, 5, 5, 5, 2, 5]
    j = [5, 5, 5, 5, 5, 5, 5, 5, 5, 2]
    assert judge_agreement.kappa(list(zip(h, j))) == pytest.approx(-1 / 9)
    assert judge_agreement.kappa([(5, 5), (2, 2), (5, 5), (1, 1)]) == 1
    assert judge_agreement.kappa([(5, 5), (5, 5)]) is None, "one class only: kappa is undefined"


def test_c_the_script_refuses_unfinished_labels_and_a_case_the_judge_lacks(tmp_path, capsys):
    labels, judged_scores = tmp_path / "h.json", tmp_path / "j.json"
    judged_scores.write_text(json.dumps({"model": "m", "rubric": "v1", "cases": {"a": scores()}}))
    labels.write_text(json.dumps({"cases": {"a": scores("TODO")}}))
    assert judge_agreement.main([str(labels), str(judged_scores)]) == 1
    assert "a: correctness" in capsys.readouterr().err
    labels.write_text(json.dumps({"cases": {"a": scores(), "b": scores()}}))
    assert judge_agreement.main([str(labels), str(judged_scores)]) == 1
    assert "no judge score for b" in capsys.readouterr().err


def test_c_the_script_prints_each_dimensions_agreement_and_the_judge_model(tmp_path, capsys):
    labels, judged_scores = tmp_path / "h.json", tmp_path / "j.json"
    labels.write_text(json.dumps({"cases": {"a": scores(5, 5, 5), "b": scores(1, 1, 1)}}))
    judged_scores.write_text(json.dumps({"model": "z-ai/glm", "rubric": "v1",
                                         "cases": {"a": scores(4, 4, 4), "b": scores(5, 1, 1)}}))
    assert judge_agreement.main([str(labels), str(judged_scores)]) == 0
    out = capsys.readouterr().out
    assert "z-ai/glm" in out and "rubric v1" in out
    assert "correctness" in out and "50%" in out and "coverage" in out and "actionability" in out


# The report: the current agent's scores against the provisional proposal ─────────────────────────
def graded_row(case_id, repeat, judgment, passed=True):
    return {"vars": {"case_id": case_id}, "repeatIndex": repeat, "success": passed, "response": {"output": {}},
            "gradingResult": {"componentResults": [
                {"pass": True, "assertion": {"value": "file://promptfoo/asserts.py:schema_valid"}},
                {"pass": passed, "reason": "x", "assertion": {"value": "file://promptfoo/asserts.py:case_expectations"}},
                {"pass": True, "metadata": judgment and {"judgment": judgment},
                 "assertion": {"value": "file://promptfoo/llm_rubric.py:judge_results"}}]}}


def judgment(correctness=4, coverage=4, actionability=4, cost=0.01):
    return {"correctness": {"score": correctness}, "coverage": {"score": coverage}, "actionability": {"score": actionability},
            "rubric": "v1", "model_requested": "z-ai/glm",
            "model_reported": "z-ai/glm", "provider": "Wafer", "cost": cost, "prompt_tokens": 1, "completion_tokens": 1}


def test_the_report_refuses_holdout_rows(tmp_path):
    held = sorted(case_rules.holdout_ids())[0]
    with pytest.raises(ValueError, match="hold-out"):
        judge_report.collect({"results": {"results": [graded_row(held, 0, judgment())]}}, [case()])


def test_the_report_gives_the_means_the_proposal_and_the_cost(capsys):
    sec = case(min_severity="high") | {"case_id": "sec-1", "category": "security"}
    sty = case("raise_nothing", "src/**") | {"case_id": "sty-1", "category": "style_only"}
    rows = [graded_row("sec-1", 0, judgment(5, 2, 5)), graded_row("sec-1", 1, judgment(3, 3, 3)),
            graded_row("sty-1", 0, judgment(None, 5, None), passed=False)]
    collected = judge_report.collect({"results": {"results": rows}}, [sec, sty])
    summary = judge_report.summarise(collected)
    assert summary["mean"]["correctness"] == 4 and summary["mean"]["coverage"] == pytest.approx(10 / 3)
    assert summary["security_misses"] == 1
    assert summary["style_only_pass_rate"] == 0
    assert summary["cost"] == pytest.approx(0.03)
    judge_report.print_summary(summary)
    out = capsys.readouterr().out
    assert "z-ai/glm" in out and "Wafer" in out and "provisional" in out


def test_the_scores_file_holds_only_the_sample_at_repeat_0():
    a, b = case() | {"case_id": "a"}, case() | {"case_id": "b"}
    collected = judge_report.collect({"results": {"results": [
        graded_row("a", 0, judgment(1, 1, 1)), graded_row("a", 1, judgment(2, 2, 2)), graded_row("b", 0, judgment())]}}, [a, b])
    out = judge_report.sample_scores(collected, ["a"])
    assert out["model"] == "z-ai/glm" and out["rubric"] == "v1"
    assert out["cases"] == {"a": {"correctness": 1, "coverage": 1, "actionability": 1}}
    with pytest.raises(ValueError, match="no judgment for c"):
        judge_report.sample_scores(collected, ["c"])


# (a), (f) ── the real judge model, run by hand ───────────────────────────────────────────────────
live = pytest.mark.skipif(os.environ.get("JUDGE_LIVE") != "1", reason="calls the real judge model: set JUDGE_LIVE=1")
OTHER_CLAIM = "The log message in start_queued has a typo and should use a named constant."
OTHER_FIX = "Fix the typo and add a LOG_MESSAGE constant."  # not the fix for CLAIM, which would bring it back


def live_judge(c, r):
    with patch.object(llm_rubric, "get_diff", return_value=DIFF), patch.object(llm_rubric, "GitHubFileReader", FileReader), \
            patch.object(llm_rubric, "github_token", return_value=""):
        graded = llm_rubric.judge_results(r, {"vars": {"case": c}})
    assert "judge_error" not in graded["metadata"], graded["metadata"]
    return graded["metadata"]["judgment"]


@live
def test_a_live_a_fabricated_finding_scores_correctness_at_most_2():
    fabricated = finding("src/queue.py opens a SQL connection at line 3 and builds the query by string concatenation, "
                         "so a key can inject SQL.")
    assert live_judge(case(min_severity="low"), review(fabricated))["correctness"]["score"] <= 2


UNREAD_CLAIM = ("loop.start in src/loop.py never writes the status file before opening the tab, so a failed start "
                "leaves the ticket in neither place.")


@live
def test_a_live_a_claim_about_code_the_agent_did_not_read_counts_as_false():
    r = review(finding(UNREAD_CLAIM, suggestion="Write the status first in loop.start."), files_read=())
    assert live_judge(case("raise_nothing", "docs/**"), r)["correctness"]["score"] <= 2


@live
def test_a_live_a_claim_the_read_file_shows_is_wrong_counts_as_false():
    r = review(finding(UNREAD_CLAIM, suggestion="Write the status first in loop.start."), files_read=("src/loop.py",))
    assert live_judge(case("raise_nothing", "docs/**"), r)["correctness"]["score"] <= 2


@live
def test_f_live_a_different_claim_in_the_window_does_not_cover_a_true_bug():
    assert live_judge(case(min_severity="low"), review(finding(OTHER_CLAIM, line=4, suggestion=OTHER_FIX)))["coverage"]["score"] <= 2


@live
def test_f_live_a_different_claim_in_the_window_is_not_the_false_claim_coming_back():
    assert live_judge(case("skip_false_bug"), review(finding(OTHER_CLAIM, line=4, suggestion=OTHER_FIX)))["coverage"]["score"] >= 3


@live
def test_f_live_the_real_claim_elsewhere_covers_a_true_bug():
    assert live_judge(case(min_severity="low"), review(finding(CLAIM, line=40)))["coverage"]["score"] >= 4


# The owner's sheet ───────────────────────────────────────────────────────────────────────────────
def test_the_labelling_sheet_is_blind_and_shows_each_commits_diff_once():
    import judge_labelling_sheet as labelling

    a, b = case(min_severity="low") | {"case_id": "a"}, case("skip_false_bug") | {"case_id": "b"}
    r = review(finding())
    context = {"diff": DIFF, "description": "Saves the queue earlier.", "files": {"src/loop.py": LOOP_PY}}
    text = labelling.sheet([(a, r), (b, r)], {SHA: context})
    assert "## a" in text and "## b" in text and CLAIM in text and "Save after start." in text
    assert text.count("save_queue(queue)") == 1 and text.count("LOOP-PY-MARKER") == 1
    assert text.count("Saves the queue earlier.") == 1 and "src/loop.py" in text
    for leak in ("judge_results", "namedScores", "case_expectations", "line window"):
        assert leak not in text


def test_the_labels_template_asks_only_for_what_needs_a_judgement():
    import judge_labelling_sheet as labelling

    empty_true = case(min_severity="low") | {"case_id": "a"}
    empty_clean = case("raise_nothing", "src/**") | {"case_id": "b"}
    full = case(min_severity="low") | {"case_id": "c"}
    cases = labelling.template([(empty_true, review()), (empty_clean, review()), (full, review(finding()))])["cases"]
    assert cases["a"] == {"correctness": None, "coverage": "TODO", "actionability": None, "note": ""}
    assert cases["b"]["coverage"] == "TODO"
    assert cases["c"]["correctness"] == "TODO" and cases["c"]["actionability"] == "TODO"
