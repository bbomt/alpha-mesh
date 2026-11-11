"""Silver labels (PMAT-326): acceptance tests (b) to (d). The canary (a) is in tests/test_canary.py."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import case_rules
import judge_agreement
import judge_sample
import llm_rubric
import silver_labels
from review_pull_request.config import Config, SENSITIVE_PATTERNS
from tests.helpers import chat_model_factory

SAMPLE = json.loads(judge_sample.SAMPLE.read_text())["cases"]
JUDGE_SCORE, LINE_WINDOW, OTHER_REPEAT = "JUDGE-SCORE-MARKER-4f1", "LINE-WINDOW-MARKER-9c2", "REPEAT-1-MARKER-77a"
DIFF = "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1 @@\n+x = 1\n"


def stored_review(case_id, marker=""):
    findings = [] if case_id.endswith("1") else [  # some reviews have no findings, to use both schemas
        {"file": "src/x.py", "line": 1, "severity": "low", "summary": f"A claim. {marker}", "suggestion": None}]
    return {"findings": findings, "model": "m", "files": ["src/x.py"], "incomplete": [], "files_read": ["src/x.py"]}


def graded(case_id, repeat, output):
    """A stored row as Promptfoo writes it: the judge's score and the line-window result sit beside the review."""
    return {"vars": {"case_id": case_id}, "repeatIndex": repeat, "response": {"output": output},
            "gradingResult": {"componentResults": [
                {"pass": False, "reason": f"line window: {LINE_WINDOW}",
                 "assertion": {"value": "file://promptfoo/asserts.py:case_expectations"}},
                {"pass": True, "namedScores": {"correctness": JUDGE_SCORE},
                 "metadata": {"judgment": {"correctness": {"why": JUDGE_SCORE, "score": 4}}},
                 "assertion": {"value": "file://promptfoo/llm_rubric.py:judge_results"}}]}}


@pytest.fixture
def run_dir(tmp_path):
    rows = [graded(c, 0, stored_review(c)) for c in SAMPLE]
    rows += [graded(c, 1, stored_review(c, OTHER_REPEAT)) for c in SAMPLE]
    rows += [graded(c, 0, stored_review(c, "HOLDOUT")) for c in sorted(case_rules.holdout_ids())]
    (tmp_path / "results.json").write_text(json.dumps({"results": {"results": rows}}))
    return tmp_path


class Reader:
    def __init__(self, *a, **k):
        pass

    def read_file(self, path, max_lines):
        return "x = 1\n"

    def close(self):
        pass


def reply(correctness=4, coverage=4, actionability=4):
    return {"correctness": {"why": "c", "score": correctness}, "coverage": {"why": "v", "score": coverage},
            "actionability": {"why": "a", "score": actionability}}


@pytest.fixture
def labelled(run_dir, tmp_path, monkeypatch):
    """silver_labels.main through a fake model; returns (exit code, prompts seen, schemas sent, outputs)."""
    scores, reasons = tmp_path / "silver.json", tmp_path / "out" / "silver.md"
    monkeypatch.setenv("SILVER_CALIBRATION_URL", "http://silver")
    monkeypatch.setenv("SILVER_CALIBRATION_TOKEN", "silver-token")
    monkeypatch.setattr(silver_labels, "SCORES", scores)
    monkeypatch.setattr(silver_labels, "REASONS", reasons)

    def run(answers, argv=(), model_argv=("--model", "openai/labeller")):
        seen, schemas, answers = [], [], iter(answers)

        def model(messages, tools):
            seen.append(json.dumps(messages))
            answer = next(answers)
            return SimpleNamespace(content=answer if isinstance(answer, str) else json.dumps(answer), tool_calls=[])

        def factory(meta, effort, schema):
            schemas.append(schema)
            meta.update(cost=0.01, model_reported="openai/labeller-2026", provider="OpenAI")
            return chat_model_factory(model)

        cfg = Config(model="z-ai/judge", api_url="http://x", api_token="judge-token", exclusions=list(SENSITIVE_PATTERNS))
        with patch.object(llm_rubric, "judge_config", return_value=cfg), \
                patch.object(llm_rubric, "get_diff", return_value=DIFF), \
                patch.object(llm_rubric, "GitHubFileReader", Reader), \
                patch.object(llm_rubric, "github_token", return_value="gh-token"), \
                patch.object(llm_rubric, "get_pr_description", return_value="The PR description."), \
                patch.object(silver_labels, "make_labeller_chat_model", factory):
            code = silver_labels.main([str(run_dir), *model_argv, *argv])
        return code, seen, schemas, scores, reasons
    return run


# (b) ── blind: no judge score, line-window result, other repeat or hold-out case reaches the labeller ──
def test_b_no_prompt_holds_a_judge_score_a_line_window_result_or_a_holdout_case(labelled):
    code, seen, _, _, _ = labelled([reply()] * 75)

    assert code == 0 and len(seen) == 75, "25 cases, 3 passes"
    prompts = "\n".join(seen)
    assert "A claim." in prompts and "The PR description." in prompts and "x = 1" in prompts, "what it must see"
    for marker in (JUDGE_SCORE, LINE_WINDOW, OTHER_REPEAT, "HOLDOUT"):
        assert marker not in prompts
    assert not [h for h in case_rules.holdout_ids() if h in prompts]


def test_b_the_schema_puts_why_before_score_in_every_dimension_and_asks_null_dimensions_nothing(labelled):
    _, _, schemas, _, _ = labelled([reply()] * 75)

    for schema in schemas:
        body = schema["json_schema"]["schema"]
        assert schema["json_schema"]["strict"] is True
        for part in body["properties"].values():
            assert list(part["properties"]) == ["why", "score"] and part["required"] == ["why", "score"]
    asked = {tuple(s["json_schema"]["schema"]["properties"]) for s in schemas}
    assert asked == {("correctness", "coverage", "actionability"), ("coverage",)}


def test_b_the_labeller_refuses_a_holdout_case():
    held = sorted(case_rules.holdout_ids())[0]
    with pytest.raises(silver_labels.LabelError, match="hold-out"):
        silver_labels.label_once({"case_id": held}, stored_review(held), "openai/labeller", "high")


def test_b_the_chat_model_sends_the_schema_and_effort_without_a_temperature():
    cfg = Config(model="openai/labeller", api_url="https://openrouter.ai/api/v1", api_token="t")
    schema = silver_labels.response_schema(True)
    chat = silver_labels.make_labeller_chat_model({}, "high", schema)(cfg)
    assert chat.temperature is None and chat.model_name == "openai/labeller"
    assert chat.extra_body == {"response_format": schema, "reasoning": {"effort": "high"},
                               "provider": {"require_parameters": True}}


# (c) ── the reply is parsed from its JSON, and a bad score is an error, never a default ──────────
def test_c_a_stubbed_reply_gives_a_scores_file_that_judge_agreement_reads(labelled, tmp_path, capsys):
    code, _, _, scores, reasons = labelled([reply()] * 75)
    assert code == 0
    silver = json.loads(scores.read_text())
    assert silver["standard"] == "silver" and silver["passes"] == 3 and silver["rubric"] == "v1"
    assert silver["labeller"] == {"requested": "openai/labeller", "reported": ["openai/labeller-2026"],
                                  "providers": ["OpenAI"]}
    assert silver["reasoning_effort"] == "high" and silver["cost_usd"] == 0.75 and silver["repeat"] == 0
    assert set(silver["cases"]) == set(SAMPLE) and "TODO" not in scores.read_text()
    empty = [c for c in SAMPLE if c.endswith("1")]
    assert empty, "the fake gives a review with no findings to cases ending in 1: the sample must have one"
    assert silver["cases"][empty[0]]["correctness"] is None and silver["cases"][empty[0]]["coverage"] == 4
    assert "why" not in scores.read_text(), "scores only: the reasons are in the gitignored .md"
    assert "Pass 3" in reasons.read_text()

    judge = tmp_path / "judge.json"
    judge.write_text(json.dumps({"model": "z-ai/judge", "rubric": "v1",
                                 "cases": {c: silver["cases"][c] for c in SAMPLE}}))
    assert judge_agreement.main(["--silver-calibration", str(scores), str(judge)]) == 0
    assert "correctness: 0 gold" in capsys.readouterr().out


@pytest.mark.parametrize("bad", [
    {"coverage": {"why": "v"}},  # missing score
    reply(coverage=6),
    reply(coverage=0),
    reply(coverage=True),
    "not json",
])
def test_c_a_missing_or_out_of_range_score_is_an_error_naming_the_case_and_pass(labelled, bad, capsys):
    code, _, _, scores, _ = labelled([reply()] * 25 + [bad])
    err = capsys.readouterr().err
    assert code == 1 and f"pass 2: {SAMPLE[0]}" in err
    assert not scores.exists(), "no partial or defaulted file"


def test_c_the_median_of_the_passes_and_the_unstable_flag():
    passes = [reply(2, 5, 4), reply(4, 5, 4), reply(5, 4, 4)]
    judged = [llm_rubric._parse(json.dumps(p), [{"file": "f"}]) for p in passes]
    assert silver_labels.combine(judged) == {"correctness": 4, "coverage": 5, "actionability": 4, "note": "",
                                             "unstable": True}
    assert silver_labels.combine(judged[1:])["unstable"] is False
    agreement = silver_labels.self_agreement({"a": judged, "b": judged[1:]})
    assert agreement["actionability"] == {"n": 2, "exact": 1.0, "within_1": 1.0}
    assert agreement["correctness"] == {"n": 2, "exact": 0.0, "within_1": 0.5}


def test_c_an_even_number_of_passes_is_refused(capsys):
    with pytest.raises(SystemExit):
        silver_labels.main(["run", "--model", "m", "--passes", "2"])
    assert "odd" in capsys.readouterr().err


def test_c_without_model_the_labeller_is_silver_model_from_env(labelled, monkeypatch):
    monkeypatch.setenv("SILVER_CALIBRATION_MODEL", "openai/from-env")
    code, _, _, scores, _ = labelled([reply()] * 75, model_argv=())
    assert code == 0 and json.loads(scores.read_text())["labeller"]["requested"] == "openai/from-env"


def test_c_with_neither_model_nor_silver_model_nothing_is_sent(labelled, monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("SILVER_CALIBRATION_MODEL", raising=False)
    monkeypatch.setattr(silver_labels.config_module, "ENV_FILE", tmp_path / "no.env")  # not the real .env
    code, seen, _, scores, _ = labelled([], model_argv=())
    assert code == 1 and "SILVER_CALIBRATION_MODEL" in capsys.readouterr().err
    assert not seen and not scores.exists()


# (d) ── gold overrides silver, per score ──────────────────────────────────────────────────────────
def scores(correctness=4, coverage=4, actionability=4):
    return {"correctness": correctness, "coverage": coverage, "actionability": actionability}


@pytest.fixture
def files(tmp_path):
    silver = {"standard": "silver", "labeller": {"requested": "openai/labeller"}, "rubric": "v1",
              "cases": {c: scores(5 if c == SAMPLE[0] else 4) for c in SAMPLE}}
    judge = {"model": "z-ai/judge", "rubric": "v1", "cases": {c: scores(2 if c == SAMPLE[0] else 4) for c in SAMPLE}}
    paths = {name: tmp_path / f"{name}.json" for name in ("silver", "judge", "gold", "todo")}
    paths["silver"].write_text(json.dumps(silver))
    paths["judge"].write_text(json.dumps(judge))
    todo = {c: scores("TODO", "TODO", "TODO") | {"note": ""} for c in SAMPLE}
    paths["todo"].write_text(json.dumps({"cases": todo}))
    paths["gold"].write_text(json.dumps({"cases": {**todo, SAMPLE[0]: {**todo[SAMPLE[0]], "correctness": 2}}}))
    return paths


def test_d_a_gold_score_replaces_silver_for_that_dimension_only(files):
    silver = json.loads(files["silver"].read_text())["cases"]
    gold = json.loads(files["gold"].read_text())["cases"]
    labels, sources, discrepancies = judge_agreement.reference(silver, gold)
    assert labels[SAMPLE[0]] == scores(2, 4, 4), "correctness from gold; the case's other dimensions stay silver"
    assert all(labels[c] == silver[c] for c in SAMPLE[1:])
    assert sources["correctness"] == {"gold": 1, "silver": 24} and sources["coverage"] == {"gold": 0, "silver": 25}
    assert discrepancies == [(SAMPLE[0], "correctness", 5, 2)]


def test_d_the_agreement_the_source_counts_and_the_discrepancy_reflect_the_override(files, capsys):
    assert judge_agreement.main(["--silver-calibration", str(files["silver"]),
                                "--gold-calibration", str(files["gold"]), str(files["judge"])]) == 0
    out = capsys.readouterr().out
    assert "correctness: 1 gold, 24 silver" in out and "coverage: 0 gold, 25 silver" in out
    assert "within ±1: 100%" in out.split("correctness")[1], "the judge's 2 now agrees with gold's 2"
    assert f"{SAMPLE[0]} correctness: silver 5, gold 2" in out


def test_d_an_all_todo_gold_file_equals_silver_only(files, capsys):
    assert judge_agreement.main(["--silver-calibration", str(files["silver"]), str(files["judge"])]) == 0
    silver_only = capsys.readouterr().out
    assert judge_agreement.main(["--silver-calibration", str(files["silver"]),
                                "--gold-calibration", str(files["todo"]), str(files["judge"])]) == 0
    assert capsys.readouterr().out == silver_only
    assert "correctness: 0 gold, 25 silver" in silver_only and "Silver vs gold" not in silver_only


def test_d_a_close_disagreement_is_not_listed(files, capsys):
    gold = json.loads(files["gold"].read_text())
    gold["cases"][SAMPLE[0]]["correctness"] = 4  # silver 5: within 1 point
    files["gold"].write_text(json.dumps(gold))
    assert judge_agreement.main(["--silver-calibration", str(files["silver"]),
                                "--gold-calibration", str(files["gold"]), str(files["judge"])]) == 0
    assert "Silver vs gold" not in capsys.readouterr().out


def test_d_unfinished_silver_and_a_missing_source_are_refused(files, capsys):
    assert judge_agreement.main(["--silver-calibration", str(files["todo"]), str(files["judge"])]) == 1
    assert "unfinished silver labels" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        judge_agreement.main([str(files["judge"])])
    assert judge_agreement.main(["--gold-calibration", str(files["todo"]), str(files["judge"])]) == 1, \
        "gold alone (no silver) must have every score set, so an all-TODO gold file is refused"


def test_c_the_labeller_goes_to_its_own_url_and_token_not_the_judge_s(labelled):
    """The judge may be on another host (DeepSeek's own API) than the labeller's model (OpenRouter)."""
    sent = []
    real_ask = llm_rubric.ask

    def ask(case, review, config, factory):
        sent.append((config.model, config.api_url, config.api_token))
        return real_ask(case, review, config, factory)

    with patch.object(llm_rubric, "ask", ask):
        code, *_ = labelled([reply()] * 75)
    assert code == 0 and sent
    assert set(sent) == {("openai/labeller", "http://silver", "silver-token")}


@pytest.mark.parametrize("var", ["SILVER_CALIBRATION_URL", "SILVER_CALIBRATION_TOKEN"])
def test_c_a_missing_silver_url_or_token_stops_before_anything_is_sent(labelled, monkeypatch, tmp_path, capsys, var):
    monkeypatch.setattr(silver_labels.config_module, "ENV_FILE", tmp_path / "no.env")  # not the real .env
    monkeypatch.setenv(var, "")
    code, seen, _, scores, _ = labelled([reply()] * 75)
    assert code == 1 and var in capsys.readouterr().err
    assert not seen and not scores.exists()
