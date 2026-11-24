"""The judgment cache (PMAT-331): judging a row whose request is identical to an earlier judgment's reuses that
judgment, with no model call; any change to the request makes a new call.

It sits in the judge's HTTP transport, so the key is the request exactly as sent: after ReviewSession's redaction,
with the model, the rubric (system message), the prompt and every parameter (temperature, provider pin, effort,
max_tokens) in its body. A hit replays the stored judgment as the model's reply, so the judge parses it as usual.

The store is one JSON file per key under ~/.mesh/judge-cache/ (outside the worktree, so it survives its removal):
the judgment (scores and why), the reported model and provider, tokens, cost and the date. Never the prompt, which
holds repo code. Only a judgment that parsed is written (the judge calls save()), so an error is retried on a rerun.
An entry unused for UNUSED_DAYS is deleted at the next run's start; a hit restarts its days.
EVAL_JUDGE_CACHE=off (run_eval.sh --no-judge-cache) re-judges every row and stores nothing.
"""

import hashlib
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import httpx

DIR = Path.home() / ".mesh" / "judge-cache"
UNUSED_DAYS = 30
STORED = ("model_reported", "provider", "prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens",
          "cost")
KEY = "_judge_cache_key"  # where the transport leaves the key in the judge's meta, for save()


def enabled() -> bool:
    return os.environ.get("EVAL_JUDGE_CACHE") != "off"


def key(request: httpx.Request, rubric_version: str) -> str:
    digest = hashlib.sha256()
    for part in (request.url.host.encode(), rubric_version.encode(), request.read()):
        digest.update(b"%d:" % len(part) + part)  # length-prefixed, so no two different requests hash alike
    return digest.hexdigest()


def _path(cache_key: str) -> Path:
    return DIR / f"{cache_key}.json"


def load(cache_key: str) -> dict | None:
    path = _path(cache_key)
    try:
        entry = json.loads(path.read_text())
        os.utime(path)  # a hit restarts its UNUSED_DAYS
    except (OSError, ValueError):  # absent, pruned meanwhile, or torn: a miss
        return None
    return entry if isinstance(entry, dict) and isinstance(entry.get("judgment"), dict) else None


def save(meta: dict, judgment: dict) -> None:
    """Store a judgment that parsed, under the key its request left in meta. A failed write only warns: the
    judgment itself is good."""
    cache_key = meta.get(KEY)
    if not cache_key or meta.get("judge_cache") == "hit":
        return
    entry = {"judgment": judgment, **{f: meta[f] for f in STORED if f in meta}, "date": date.today().isoformat()}
    tmp = DIR / f".{cache_key}.{os.getpid()}.tmp"
    try:
        DIR.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(entry, indent=1))
        os.replace(tmp, _path(cache_key))  # atomic: a concurrent reader sees the whole entry or none
    except OSError as e:
        print(f"WARNING: judge cache: could not store a judgment: {e}", file=sys.stderr)


def prune(now: float | None = None) -> None:
    cutoff = (now or time.time()) - UNUSED_DAYS * 86400
    for path in DIR.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:  # removed by another run meanwhile
            pass


class CachingTransport(httpx.BaseTransport):
    """On a hit, answers with the stored judgment and sends nothing; on a miss, notes the key and sends."""

    def __init__(self, inner: httpx.BaseTransport, meta: dict, rubric_version: str) -> None:
        self._inner, self._meta, self._rubric = inner, meta, rubric_version

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        cache_key = key(request, self._rubric)
        self._meta[KEY] = cache_key
        entry = load(cache_key)
        if entry is None:
            return self._inner.handle_request(request)
        # No tokens or cost: this row spent none. The stored entry keeps what the original call spent.
        self._meta.update({f: entry[f] for f in ("model_reported", "provider") if entry.get(f)},
                          judge_cache="hit", judged_on=entry.get("date"))
        chunk = {"id": "judge-cache", "object": "chat.completion.chunk", "created": 0,
                 "model": entry.get("model_reported") or "", "choices": [
                     {"index": 0, "delta": {"role": "assistant", "content": json.dumps(entry["judgment"])},
                      "finish_reason": "stop"}]}
        body = b"data: " + json.dumps(chunk).encode() + b"\n\ndata: [DONE]\n\n"
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=httpx.ByteStream(body),
                              request=request)

    def close(self) -> None:
        self._inner.close()
