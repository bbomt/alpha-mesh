"""The LLM judge (PMAT-311): a Promptfoo python assert, `file://promptfoo/llm_rubric.py:judge_results`.

An LLM-as-a-judge check with a rubric (judge/rubric.v1.md), written as a python assert instead of Promptfoo's
built-in llm-rubric (owner, 2026-11-06) because it has to go through ReviewSession: an excluded file's content never
reaches a model, by any path. It also returns its three scores from one call.

The judge sees what the agent saw (owner, PMAT-311): the diff, the PR description and the files the agent read, fetched
from the review's trace: what each read_file call returned, exactly as the agent got it (PMAT-325). It has no tools.
A review without a trace (a run from before PMAT-325) refetches the files in files_read at the same commit with the
agent's own line limit, until PMAT-334 makes it an error. A review that can't be judged on the agent's context (a
trace that is gone or lacks a reviewed file, no files_read, a file that can't be fetched again) is a recorded judge
error, not a score.

The judge never gates: judge_results always passes, and a judge error is recorded, not failed. Hold-out cases are
refused here, so nothing about them reaches it: rubric iterations must not use them. Thresholds are
provisional until the owner sets them (eval/README.md). EVAL_JUDGE_MODEL, EVAL_JUDGE_URL and EVAL_JUDGE_TOKEN come from
.env.
"""

import json
import os
import re
import sys
from argparse import Namespace
from dataclasses import replace

import httpx
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

from case_rules import EVAL, holdout_ids
from review_pull_request import config as config_module
from review_pull_request import llm
from review_pull_request.git import get_pr_description
from review_pull_request.github import _get_github_token
from review_pull_request.incremental import get_diff
from review_pull_request.llm import LLMError
from review_pull_request.policy import PathPolicy, normalise
from review_pull_request.readers import PolicyFileReader, ReadError, _safe_name
from review_pull_request.session import ReviewSession, _sent, http_transport
from review_pull_request.stage2 import ALREADY_READ_NOTE, NOT_FOUND_AGAIN_NOTE, READ_LIMIT_NOTE
from review_pull_request.tools import GitHubFileReader
from review_pull_request.trace import load as load_trace
from review_pull_request.watchdog import WatchdogTransport

RUBRIC_VERSION = "v1"  # a change to the rubric is a new file and a new version
RUBRIC = EVAL / "judge" / f"rubric.{RUBRIC_VERSION}.md"
REQUIRED = ("EVAL_JUDGE_MODEL", "EVAL_JUDGE_URL", "EVAL_JUDGE_TOKEN")
DIMENSIONS = ("correctness", "coverage", "actionability")


class JudgeConfigError(Exception):
    """An EVAL_JUDGE_* variable is missing."""


class JudgeReplyError(Exception):
    """The judge's reply is not the JSON the rubric asks for."""


def _require_env() -> None:
    load_dotenv(config_module.ENV_FILE)
    for var in REQUIRED:
        if not os.environ.get(var):
            raise JudgeConfigError(f"ERROR: {var} is not set. Set it in {config_module.ENV_FILE} (see .env.example).")


def _family(model: str) -> str:
    return model.split("/", 1)[0]


def startup_check() -> None:
    """Fail before any review if the judge can't run; print its model, and warn on self-preference bias."""
    _require_env()
    judge, pin = os.environ["EVAL_JUDGE_MODEL"], judge_extra_body()  # a pin off OpenRouter stops the run here
    host = f", host {pin['provider']['order'][0]} only" if pin else ""
    print(f"Judge model: {judge} (rubric {RUBRIC_VERSION}{host})", file=sys.stderr)
    for label, model in (("the run's model", os.environ.get("EVAL_MODEL")),
                         ("[model] review", config_module.configured_model("review"))):
        if model and _family(model) == _family(judge):
            print(f"WARNING: EVAL_JUDGE_MODEL {judge} and {label} {model} share a family: a model may favour its own "
                  "output. Scores for reviews by that family are not comparable.", file=sys.stderr)


def judge_config():
    """The agent's config (for its exclusions), pointed at the judge's model."""
    _require_env()
    try:
        config = config_module.load_config(Namespace(model=None, base=None, config=None, repo_path=None, dry_run=False,
                                                      verbose=False, hide_pr_comment=False))
    except SystemExit as e:  # load_config exits on a missing .env value; its message is on stderr
        raise RuntimeError(f"cannot load the review config (exit {e.code}): see stderr") from e
    return replace(config, model=os.environ["EVAL_JUDGE_MODEL"], api_url=os.environ["EVAL_JUDGE_URL"],
                   api_token=os.environ["EVAL_JUDGE_TOKEN"])


def github_token() -> str:
    token = _get_github_token() or ""
    if not token:
        raise RuntimeError("no GitHub token: run 'gh auth login' (or set GITHUB_TOKEN)")
    return token


class _RecordingStream(httpx.SyncByteStream):
    """Passes a streamed reply through unchanged, noting what the API reports in meta line by line:
    the OpenAI client stops reading at [DONE], so nothing after the last chunk would ever run."""

    def __init__(self, inner, meta: dict) -> None:
        self._inner, self._meta = inner, meta

    def __iter__(self):
        partial = b""
        for chunk in self._inner:
            *lines, partial = (partial + chunk).split(b"\n")
            for line in lines:
                if line.startswith(b"data: {"):
                    self._note(line[len(b"data: "):])
            yield chunk

    def _note(self, text: bytes) -> None:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return
        usage = data.get("usage") or {}
        # Cached prompt tokens: DeepSeek's own API names them prompt_cache_hit_tokens, OpenRouter prompt_tokens_details.
        cached = usage.get("prompt_cache_hit_tokens")
        if cached is None:
            cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        for key, value in (("model_reported", data.get("model")), ("provider", data.get("provider")),
                           ("prompt_tokens", usage.get("prompt_tokens")), ("cached_tokens", cached),
                           ("completion_tokens", usage.get("completion_tokens")),
                           ("reasoning_tokens", (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")),
                           ("cost", usage.get("cost"))):
            if value is not None:
                self._meta[key] = value

    def close(self) -> None:
        self._inner.close()


class RecordingTransport(httpx.BaseTransport):
    """LangChain drops OpenRouter's provider and cost, so they are read from the reply as it passes."""

    def __init__(self, inner: httpx.BaseTransport, meta: dict) -> None:
        self._inner, self._meta = inner, meta

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        # The stream is read here, before httpx decodes it, so it must arrive uncompressed (DeepSeek's API sends br).
        request.headers["Accept-Encoding"] = "identity"
        response = self._inner.handle_request(request)
        response.stream = _RecordingStream(response.stream, self._meta)
        return response

    def close(self) -> None:
        self._inner.close()


def judge_extra_body() -> dict | None:
    """EVAL_JUDGE_PROVIDER (optional, OpenRouter only): the one host OpenRouter may send the judge to, with no
    fallback, so every judgment comes from the same weights; a host that is down gives judge errors, not another
    host's scores. Unset: OpenRouter routes each call as it likes (the report lists the hosts)."""
    host = os.environ.get("EVAL_JUDGE_PROVIDER")
    if not host:
        return None
    if "openrouter.ai" not in os.environ.get("EVAL_JUDGE_URL", ""):
        raise JudgeConfigError("EVAL_JUDGE_PROVIDER pins an OpenRouter host: EVAL_JUDGE_URL is not OpenRouter")
    return {"provider": {"order": [host], "allow_fallbacks": False}}


def make_judge_chat_model(meta: dict, temperature: float | None = 0, extra_body: dict | None = None):
    """A chat_model_factory for ReviewSession: as session.make_chat_model, at temperature 0, recording into meta.
    The silver labeller (PMAT-326) sets no temperature and adds its schema and reasoning effort in extra_body."""
    def factory(config, model=None):
        name, base_url = _sent(config, model or config.model)
        client = httpx.Client(transport=WatchdogTransport(RecordingTransport(http_transport(base_url), meta)),
                              timeout=httpx.Timeout(llm.IDLE_TIMEOUT, connect=30))
        return ChatOpenAI(model=name, api_key=config.api_token, base_url=base_url, temperature=temperature,
                          streaming=True, stream_usage=True, max_retries=0, http_client=client, extra_body=extra_body)
    return factory


def file_reader(case: dict, token: str):
    """What the agent read from: the repo at the case's commit."""
    owner, repo = case["repo"].split("/")
    return GitHubFileReader(owner, repo, case["raised_sha"], token)


NOT_A_READ = (ALREADY_READ_NOTE, NOT_FOUND_AGAIN_NOTE, READ_LIMIT_NOTE)  # notes about a repeat, not the path's result


def traced_files(review: dict, policy: PathPolicy) -> dict:
    """{path: what the agent's first read of it returned}, from the review's trace, in the order read.
    A repeat's note is skipped (the first result is the one the agent kept), and an excluded or unsafe path is
    dropped: a trace is written behind the review's guard, but maybe under other exclusions."""
    if not os.path.isfile(review["trace"]):
        raise RuntimeError(f"the review's trace {review['trace']} is gone: not judged")
    spans = load_trace(review["trace"])
    missing = [f for f in review.get("files", []) if f not in spans]
    if missing:
        raise RuntimeError(f"the review's trace has no review of {', '.join(missing)}: not judged")
    files = {}
    for span in spans.values():
        for call in span["tools"]:
            path = (call.get("input") or {}).get("path") if isinstance(call.get("input"), dict) else None
            if (call.get("name") != "read_file" or not isinstance(path, str) or not _safe_name(path)
                    or call.get("output") in NOT_A_READ or not isinstance(call.get("output"), str)):
                continue
            path = normalise(path)
            if path not in files and not policy.is_excluded(path):
                files[path] = call["output"]
    return files


def agent_context(case: dict, review: dict, reader, policy: PathPolicy, max_lines: int, token: str) -> dict:
    """The PR description and the files the agent read: {"description", "files": {path: content}}.
    From the review's trace if it has one, with no read. Otherwise refetched through `reader`, a PolicyFileReader:
    an excluded path is dropped, as the agent could not have read it either, and a file the agent read that can't
    be fetched now is an error: judging without it would not be the agent's context."""
    if not review.get("trace") and "files_read" not in review:
        raise RuntimeError("the review has no files_read, so the judge can't see what the agent saw: "
                           "use a run from PMAT-322 or later")
    description = case.get("description")  # as agent_provider: a fixture commit states its own, a PR's is today's
    if description is None:
        description = get_pr_description(f"https://github.com/{case['repo']}/pull/{case['pr_number']}", token) \
            if case.get("pr_number") else ""
    if review.get("trace"):
        return {"description": description, "files": traced_files(review, policy)}
    paths = [p for p in review["files_read"] if not policy.is_excluded(p)]
    files = {p: reader.read_file(p, max_lines) for p in paths}
    failed = [p for p, content in files.items() if isinstance(content, ReadError)]
    if failed:
        raise RuntimeError(f"could not fetch {', '.join(failed)}, which the agent read: not judged, rerun later")
    return {"description": description, "files": files}


def _files_read(files: dict) -> str:
    if not files:
        return "The agent read no files."
    return "\n".join(f'<file path="{path}">\n{content}\n</file>' for path, content in files.items())


def _prompt(case: dict, findings: list[dict], diff: str, context: dict) -> str:
    truth = {"expected": case["expected"], "finding": case.get("finding"), "category": case["category"],
             "notes": case.get("notes"), "label_reason": case.get("label_reason")}
    numbered = [{"number": i, **f} for i, f in enumerate(findings)]
    # What the cases of one review share comes first, the case last: their calls then share a cached prefix.
    return (f"<pr_description>\n{context['description']}\n</pr_description>\n\n"
            f"<diff>\n{diff}\n</diff>\n\n"
            f"<files_read>\n{_files_read(context['files'])}\n</files_read>\n\n"
            f"<findings>\n{json.dumps(numbered, indent=1)}\n</findings>\n\n"
            f"<case>\n{json.dumps(truth, indent=1)}\n</case>")


def _score(reply: dict, dimension: str, nullable: bool):
    part = reply.get(dimension)
    score = part.get("score", "missing") if isinstance(part, dict) else "missing"
    if nullable:
        return None
    if isinstance(score, bool) or score not in (1, 2, 3, 4, 5):
        raise JudgeReplyError(f"{dimension}: score must be 1 to 5, got {score!r}")
    return score


def _parse(text: str, findings: list[dict]) -> dict:
    fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", text or "", re.DOTALL)
    try:
        reply = json.loads(fenced.group(1) if fenced else text)
    except (json.JSONDecodeError, TypeError) as e:
        raise JudgeReplyError(f"the reply is not JSON: {e}") from e
    if not isinstance(reply, dict):
        raise JudgeReplyError("the reply is not a JSON object")
    judgment = {}
    for dimension in DIMENSIONS:
        nullable = dimension != "coverage" and not findings
        score = _score(reply, dimension, nullable)
        judgment[dimension] = {"why": (reply.get(dimension) or {}).get("why"), "score": score}
    return judgment


def judge_review(case: dict, review: dict) -> dict:
    """One judgment of one stored or fresh review."""
    config, meta = judge_config(), {}
    if case["expected"]["action"] == "raise_nothing" and not review["findings"]:
        # The rubric fixes every score: coverage 5 (it raised nothing), and no findings to grade. No model call.
        fixed = {"why": "fixed by the rubric: a raise_nothing case whose review has no findings", "score": None}
        return {**{d: fixed for d in DIMENSIONS}, "coverage": {**fixed, "score": 5}, "fixed_by_rubric": True,
                "rubric": RUBRIC_VERSION, "model_requested": config.model}
    text, findings = ask(case, review, config, make_judge_chat_model(meta, extra_body=judge_extra_body()))
    return {**_parse(text, findings), "rubric": RUBRIC_VERSION, "model_requested": config.model, **meta}


def ask(case: dict, review: dict, config, chat_model_factory) -> tuple[str, list[dict]]:
    """The rubric and what the agent saw, sent to `config.model`: (the reply's text, the findings it graded).
    Everything reaches the model through ReviewSession. Shared by the judge and the silver labeller (PMAT-326)."""
    token = github_token()
    # Its session redacts these as well as its own token.
    session = ReviewSession.create(config, inner_reader=file_reader(case, token),
                                   chat_model_factory=chat_model_factory,
                                   secrets=[token, os.environ.get("CODE_REVIEW_TOKEN", "")])
    try:
        context = agent_context(case, review, session.reader, session.policy, config.max_file_lines, token)
    finally:
        session.reader.close()
    owner, repo = case["repo"].split("/")
    diff = session.safe_diff(get_diff(owner, repo, case["base_sha"], case["raised_sha"], token))
    findings = [f for f in review["findings"] if not session.policy.is_excluded(f["file"])]
    reply = session.review_llm.chat([{"role": "system", "content": RUBRIC.read_text()},
                                     {"role": "user", "content": _prompt(case, findings, diff.text, context)}])
    return reply.content, findings


def judge_results(output, context) -> dict:
    """The assert: always passes (no gate yet); the scores are in namedScores and metadata.judgment."""
    review, case = json.loads(output) if isinstance(output, str) else output, context["vars"]["case"]
    if case["case_id"] in holdout_ids():
        return {"pass": True, "score": 1, "reason": "not judged: a hold-out case"}
    repeat = review.get("repeat") or 0
    if os.environ.get("EVAL_JUDGE_REPEATS") == "first" and repeat > 0:  # while iterating (CLAUDE.md, Evals)
        return {"pass": True, "score": 1, "reason": f"not judged: repeat {repeat} (--judge-repeats first: repeat 0 only)",
                "metadata": {"judge_skipped": f"repeat {repeat}"}}
    try:
        judgment = judge_review(case, review)
    except (JudgeReplyError, LLMError, RuntimeError, httpx.HTTPError) as e:
        return {"pass": True, "score": 1, "reason": f"judge_error: {e}", "metadata": {"judge_error": str(e)}}
    scores = {d: judgment[d]["score"] for d in DIMENSIONS if judgment[d]["score"] is not None}
    return {"pass": True, "score": 1, "namedScores": scores, "metadata": {"judgment": judgment},
            "reason": f"judge {RUBRIC_VERSION}: {scores}: " + json.dumps(judgment)}
