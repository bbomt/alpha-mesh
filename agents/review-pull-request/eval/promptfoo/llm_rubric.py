"""The LLM judge (PMAT-311): a Promptfoo python assert, `file://promptfoo/llm_rubric.py:judge_results`.

An LLM-as-a-judge check with a rubric (judge/rubric.v1.md), written as a python assert instead of Promptfoo's
built-in llm-rubric (owner, 2026-11-06) because it has to go through ReviewSession: an excluded file's content never
reaches a model, by any path. It also returns its three scores from one call.

The judge sees what the agent saw (owner, PMAT-311): the diff, the PR description and the files the agent read, fetched
at the same commit with the agent's own line limit. It has no tools. A review with no files_read, or a file the agent
read that can't be fetched again, can't be judged fairly, so it is a recorded judge error, not a score. Refetching
repeats the agent's tool calls; a trace of its calls and results will replace it (PMAT-325).

The judge never gates: judge_results always passes, and a judge error is recorded, not failed. Hold-out cases are
refused here, so nothing about them reaches it: rubric iterations must not use them. Thresholds are
provisional until the owner sets them (eval/README.md). JUDGE_MODEL, JUDGE_URL and JUDGE_TOKEN come from .env.
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
from review_pull_request.policy import PathPolicy
from review_pull_request.readers import PolicyFileReader, ReadError
from review_pull_request.session import ReviewSession, _sent, http_transport
from review_pull_request.tools import GitHubFileReader
from review_pull_request.watchdog import WatchdogTransport

RUBRIC_VERSION = "v1"  # a change to the rubric is a new file and a new version
RUBRIC = EVAL / "judge" / f"rubric.{RUBRIC_VERSION}.md"
REQUIRED = ("JUDGE_MODEL", "JUDGE_URL", "JUDGE_TOKEN")
DIMENSIONS = ("correctness", "coverage", "actionability")


class JudgeConfigError(Exception):
    """A JUDGE_* variable is missing."""


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
    judge = os.environ["JUDGE_MODEL"]
    print(f"Judge model: {judge} (rubric {RUBRIC_VERSION})", file=sys.stderr)
    for label, model in (("the run's model", os.environ.get("EVAL_MODEL")),
                         ("CODE_REVIEW_MODEL", os.environ.get("CODE_REVIEW_MODEL"))):
        if model and _family(model) == _family(judge):
            print(f"WARNING: JUDGE_MODEL {judge} and {label} {model} share a family: a model may favour its own "
                  "output. Scores for reviews by that family are not comparable.", file=sys.stderr)


def judge_config():
    """The agent's config (for its exclusions), pointed at the judge's model."""
    _require_env()
    try:
        config = config_module.load_config(Namespace(model=None, base=None, config=None, repo_path=None, dry_run=False,
                                                      verbose=False, hide_pr_comment=False))
    except SystemExit as e:  # load_config exits on a missing .env value; its message is on stderr
        raise RuntimeError(f"cannot load the review config (exit {e.code}): see stderr") from e
    return replace(config, model=os.environ["JUDGE_MODEL"], api_url=os.environ["JUDGE_URL"],
                   api_token=os.environ["JUDGE_TOKEN"])


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
        for key, value in (("model_reported", data.get("model")), ("provider", data.get("provider")),
                           ("prompt_tokens", usage.get("prompt_tokens")),
                           ("completion_tokens", usage.get("completion_tokens")), ("cost", usage.get("cost"))):
            if value is not None:
                self._meta[key] = value

    def close(self) -> None:
        self._inner.close()


class RecordingTransport(httpx.BaseTransport):
    """LangChain drops OpenRouter's provider and cost, so they are read from the reply as it passes."""

    def __init__(self, inner: httpx.BaseTransport, meta: dict) -> None:
        self._inner, self._meta = inner, meta

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        response.stream = _RecordingStream(response.stream, self._meta)
        return response

    def close(self) -> None:
        self._inner.close()


def make_judge_chat_model(meta: dict):
    """A chat_model_factory for ReviewSession: as session.make_chat_model, at temperature 0, recording into meta."""
    def factory(config, model=None):
        name, base_url = _sent(config, model or config.model)
        client = httpx.Client(transport=WatchdogTransport(RecordingTransport(http_transport(base_url), meta)),
                              timeout=httpx.Timeout(llm.IDLE_TIMEOUT, connect=30))
        return ChatOpenAI(model=name, api_key=config.api_token, base_url=base_url, temperature=0,
                          streaming=True, max_retries=0, http_client=client)
    return factory


def file_reader(case: dict, token: str):
    """What the agent read from: the repo at the case's commit."""
    owner, repo = case["repo"].split("/")
    return GitHubFileReader(owner, repo, case["raised_sha"], token)


def agent_context(case: dict, review: dict, reader, policy: PathPolicy, max_lines: int, token: str) -> dict:
    """The PR description and the files the agent read: {"description", "files": {path: content}}.
    `reader` is a PolicyFileReader, and an excluded path is dropped: the agent could not have read it either.
    A file the agent read that can't be fetched now is an error: judging without it would not be the agent's context."""
    if "files_read" not in review:
        raise RuntimeError("the review has no files_read, so the judge can't see what the agent saw: "
                           "use a run from PMAT-322 or later")
    description = case.get("description")  # as agent_provider: a fixture commit states its own, a PR's is today's
    if description is None:
        description = get_pr_description(f"https://github.com/{case['repo']}/pull/{case['pr_number']}", token) \
            if case.get("pr_number") else ""
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
    return (f"<case>\n{json.dumps(truth, indent=1)}\n</case>\n\n"
            f"<findings>\n{json.dumps(numbered, indent=1)}\n</findings>\n\n"
            f"<pr_description>\n{context['description']}\n</pr_description>\n\n"
            f"<diff>\n{diff}\n</diff>\n\n"
            f"<files_read>\n{_files_read(context['files'])}\n</files_read>")


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
    """One judgment of one stored or fresh review. Everything reaches the model through ReviewSession."""
    config, token, meta = judge_config(), github_token(), {}
    # Its session redacts these as well as its own token.
    session = ReviewSession.create(config, inner_reader=file_reader(case, token),
                                   chat_model_factory=make_judge_chat_model(meta),
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
    return {**_parse(reply.content, findings), "rubric": RUBRIC_VERSION, "model_requested": config.model, **meta}


def judge_results(output, context) -> dict:
    """The assert: always passes (no gate yet); the scores are in namedScores and metadata.judgment."""
    review, case = json.loads(output) if isinstance(output, str) else output, context["vars"]["case"]
    if case["case_id"] in holdout_ids():
        return {"pass": True, "score": 1, "reason": "not judged: a hold-out case"}
    try:
        judgment = judge_review(case, review)
    except (JudgeReplyError, LLMError, RuntimeError, httpx.HTTPError) as e:
        return {"pass": True, "score": 1, "reason": f"judge_error: {e}", "metadata": {"judge_error": str(e)}}
    scores = {d: judgment[d]["score"] for d in DIMENSIONS if judgment[d]["score"] is not None}
    return {"pass": True, "score": 1, "namedScores": scores, "metadata": {"judgment": judgment},
            "reason": f"judge {RUBRIC_VERSION}: {scores}: " + json.dumps(judgment)}
