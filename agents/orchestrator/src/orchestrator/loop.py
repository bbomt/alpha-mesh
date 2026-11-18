"""The mesh orchestrator: starts a worker (a Claude session in its own tmux tab) per ticket and, each tick, does
what the owner would do by hand in that tab. The worker runs the ticket itself through the Claude harness
(worktree, branch, labels, PR, Done); the orchestrator only reads Jira and never writes it.
"""

import os
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from orchestrator import status
from orchestrator.jira import Ticket

AGENT_DIR = Path(__file__).resolve().parents[2]
# The workers' settings (hooks, sandbox). The template names the AWS region as ${AWS_REGION}, so no region is
# committed (PMAT-347); each start or resume writes the filled-in copy that --settings points at.
SETTINGS_TEMPLATE = AGENT_DIR / "settings.template.json"
REGION = re.compile(r"[a-z]{2}(-[a-z]+)+-\d")
# How an orchestrated worker differs from a manual session (Jira questions, comment marker, worktree);
# passed on every start and resume, as a system prompt does not survive the process.
WORKER_PROMPT = AGENT_DIR / "worker-prompt.md"

START = "Work on Jira {key}."
NUDGE = ("You were interrupted (the machine or this tab stopped). Check where you were (git status, the "
         "ticket, your last steps) and carry on, without redoing finished work.")
# Names the range, as the worker may have posted since the last one it was told about (so "since my last
# post" could miss one).
REPLY = "The owner commented on Jira {key}: comments after {after}, newest {comment}. Read them all and act on them."
CLOSING = ("Mesh orchestrator: Jira {key} is Done, so I will close this tab in {minutes} minutes. To keep it, type anything "
           "here before then. No reply needed.")
CLOSE_GRACE = timedelta(minutes=5)

# Hook states in which the worker sits at its prompt, so typing into the tab is safe.
AT_PROMPT = ("idle", "waiting")
# Typed text counts as received only once the worker's UserPromptSubmit hook records it, within this long. It can
# be lost (PMAT-337): if the owner left the tab's input in Claude Code's history mode, the Enter submits nothing.
CONFIRM = timedelta(seconds=15)
# Lost this many times in a row, the orchestrator stops typing it and the screen asks the owner to check the tab.
MAX_ATTEMPTS = 3


@dataclass
class Ops:
    """The outside world, so tests can fake it."""
    windows: Callable[[], set[str]]
    new_window: Callable[[str, str, str], None]
    send: Callable[[str, str], None]
    kill: Callable[[str], None]
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    background: Callable[[], set[str]] = set  # tabs with a background command still running (screen only)
    prune: Callable[[str], None] = lambda folder: None  # clear git's records of removed worktrees in a repo
    sleep: Callable[[float], None] = time.sleep


def settings_path() -> Path:
    """Where the workers' filled-in settings file goes (next to the status files)."""
    return status.DIR / "worker-settings.json"


def aws_region() -> str:
    """AWS_REGION, else AWS_DEFAULT_REGION, else the AWS profile's region (as boto3 and the AWS CLI find it)."""
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        try:
            region = subprocess.run(["aws", "configure", "get", "region"], capture_output=True, text=True).stdout.strip()
        except FileNotFoundError:
            region = ""
    if not REGION.fullmatch(region or ""):
        raise RuntimeError(f"no AWS region for the workers' sandbox (got {region!r}): set AWS_REGION, or the region "
                           "of your AWS profile (aws configure set region <region>)")
    return region


def write_settings() -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SETTINGS_TEMPLATE.read_text().replace("${AWS_REGION}", aws_region()))
    return path


def _claude(key: str, args: str) -> str:
    return (f"ORCH_TICKET={shlex.quote(key)} claude {args} --settings {shlex.quote(str(write_settings()))} "
            f"--append-system-prompt-file {shlex.quote(str(WORKER_PROMPT))}")


def start(key: str, repo_dir: str, ops: Ops) -> None:
    """Open the ticket's tab with a new worker. The session ID is chosen here and saved first, so the
    worker can be resumed even if it never got as far as its first hook."""
    session_id = str(uuid.uuid4())
    status.update(key, ticket=key, session_id=session_id, launch_dir=repo_dir)
    ops.new_window(key, repo_dir, _claude(key, f"--name {shlex.quote(key)} --session-id {session_id} "
                                               f"{shlex.quote(START.format(key=key))}"))


def tick(keys: list[str], tickets: dict[str, Ticket], ops: Ops) -> list[str]:
    """One pass over the active tickets; returns what was done, for the log. Each ticket is handled on its
    own, so one ticket's failure (e.g. a tab gone mid-tick) does not starve the others."""
    done, alive = [], ops.windows()
    for key in keys:
        try:
            if line := _tick_one(key, key in alive, tickets.get(key), ops):
                done.append(line)
        except Exception as e:
            done.append(f"{key}: failed this tick, trying again next tick: {e!r}")
    return done


def _prompts_since(s: dict, since: str) -> int:
    """Prompts the worker received (UserPromptSubmit hooks) at or after an ISO time."""
    t = datetime.fromisoformat(since)
    return sum(1 for h in s.get("history", [])
               if h.get("event") == "UserPromptSubmit" and datetime.fromisoformat(h["at"]) >= t)


def _send_confirmed(key: str, text: str, ops: Ops) -> str | None:
    """Type text into the tab, then wait for the worker to receive it as a prompt. Returns the time it was typed
    (to the second, as hook times are) if it arrived, else None. The input is never cleared first: it may hold
    the owner's draft, and in Claude Code a double Esc opens the rewind menu."""
    sent = ops.now().replace(microsecond=0)
    ops.send(key, text)
    while not _prompts_since(status.read(key), sent.isoformat()):
        if ops.now() - sent >= CONFIRM:
            return None
        ops.sleep(1)
    return sent.isoformat()


def _gave_up(key: str, s: dict, what: str) -> bool:
    """True while `what` has been lost MAX_ATTEMPTS times and the tab has received no prompt since: typing it
    again would be lost too. Once a prompt gets through (the owner fixed the tab), it is typed again."""
    u = s.get("undelivered") or {}
    if u.get("what") != what or u["attempts"] < MAX_ATTEMPTS:
        return False
    if _prompts_since(s, u["at"]):
        status.update(key, undelivered=None)
        return False
    return True


def _lost(key: str, s: dict, what: str, ops: Ops) -> str:
    """Count a lost attempt; the next check at the prompt tries again, until MAX_ATTEMPTS."""
    u = s.get("undelivered") or {}
    attempts = (u["attempts"] if u.get("what") == what else 0) + 1
    status.update(key, undelivered={"what": what, "attempts": attempts,
                                    "at": ops.now().isoformat(timespec="seconds")})
    if attempts >= MAX_ATTEMPTS:
        return f"{key}: {what} not delivered: check the tab"
    return f"{key}: {what} not received (the tab's input may be in use), trying again next check"


def _tick_one(key: str, alive: bool, t: Ticket | None, ops: Ops) -> str | None:
    s = status.read(key)
    if not alive:
        # Run from the launch folder: Claude finds a session to resume by the folder it started in.
        ops.new_window(key, s["launch_dir"], _claude(key, f"--resume {s['session_id']} {shlex.quote(NUDGE)}"))
        # A close warning from before is void: the resume message would count as the owner typing.
        status.update(key, close_warned_at=None)
        return f"{key}: tab missing, resumed the worker"
    if t is None:
        return None
    if "relayed_comment" not in s:
        # First look at this ticket: comments made before the worker started are not news.
        status.update(key, relayed_comment=t.comment_id)
        return None
    if s.get("state") not in AT_PROMPT:
        return None
    # Any new owner comment is passed on, whatever the labels: an answer, a rejected merge, feedback.
    if t.comment_id and t.comment_id != s["relayed_comment"]:
        what = f"comment {t.comment_id}"
        if _gave_up(key, s, what):
            return None
        after = s["relayed_comment"] or "the ones from before you started"
        if not _send_confirmed(key, REPLY.format(key=key, after=after, comment=t.comment_id), ops):
            return _lost(key, s, what, ops)  # relayed_comment unchanged, so the next check types it again
        status.update(key, relayed_comment=t.comment_id, undelivered=None)
        return f"{key}: passed on the owner's comment {t.comment_id}"
    if t.done and "AWAITING-USER-RESPONSE" not in t.labels:
        # Done at the prompt with no open question: finished. Warn in the tab first, so an owner working in it
        # can keep it. (The owner merges and tells the worker, on Jira or in its tab.)
        if not (warned := s.get("close_warned_at")):
            # The grace period starts only once the warning arrived: a tab whose input is in use is never closed.
            if _gave_up(key, s, "the closing warning"):
                return None
            if not (sent := _send_confirmed(key, CLOSING.format(key=key, minutes=CLOSE_GRACE.seconds // 60), ops)):
                return _lost(key, s, "the closing warning", ops)
            status.update(key, close_warned_at=sent, undelivered=None)
            return f"{key}: Done, warned the tab it closes in {CLOSE_GRACE.seconds // 60} minutes"
        if _prompts_since(s, warned) > 1:  # our warning, then the owner typed
            status.update(key, finished=True, kept_by_owner=True)
            return f"{key}: Done, the owner kept the tab; it is theirs now"
        if ops.now() - datetime.fromisoformat(warned) >= CLOSE_GRACE:
            ops.kill(key)
            status.update(key, finished=True)
            # The worker removed its worktree inside its sandbox, which mounts git's record of each worktree
            # (.git/worktrees/<name>) read-only, so the record is left behind. Prune only drops records whose
            # worktree folder is gone, so live worktrees are untouched.
            ops.prune(s["launch_dir"])
            return f"{key}: Done, closed its tab"
    return None
