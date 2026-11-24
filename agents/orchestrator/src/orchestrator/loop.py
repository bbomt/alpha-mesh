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
# A pause (PMAT-355: a lowered --max-workers, or room for the --priority ticket) warns the same way, with the same grace.
PAUSING = ("Mesh orchestrator: I am pausing this worker ({reason}) and will close this tab in {minutes} minutes; "
           "this same session is resumed later. Now: stop any running processes (background commands, servers, "
           "watchers), then post a MESSAGE-FROM-WORKER comment on Jira {key} with your current status: that you are "
           "paused by the orchestrator, where you stopped, and your next step, so you can resume from it. "
           "(Owner: to keep this tab running, type anything here before then.)")
RESUMED = ("Mesh orchestrator: you were paused (your tab was closed) and are now resumed in the same session. "
           "{comment} Check where you were (git status, the ticket) and carry on, without redoing finished work.")
RESUMED_COMMENT = "Read your Jira comment {comment} on {key} first: your status when you were paused."
RESUMED_NO_COMMENT = "You posted no Jira comment when paused; read your last comments on {key} first."

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
            # A list (no shell); a timeout, so a hung CLI cannot stall every ticket's tick.
            region = subprocess.run(["aws", "configure", "get", "region"], capture_output=True, text=True,
                                    timeout=10).stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
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
    status.update(key, ticket=key, session_id=session_id, launch_dir=repo_dir,
                  started_at=ops.now().isoformat())
    ops.new_window(key, repo_dir, _claude(key, f"--name {shlex.quote(key)} --session-id {session_id} "
                                               f"{shlex.quote(START.format(key=key))}"))


def resume_paused(key: str, ops: Ops) -> None:
    """Reopen a paused ticket's tab, resuming its same Claude session in its own folder (never afresh)."""
    s = status.read(key)
    comment = (RESUMED_COMMENT.format(comment=s["pause_comment"], key=key) if s.get("pause_comment")
               else RESUMED_NO_COMMENT.format(key=key))
    # Saved first, like start(): from here it is active, so a failed new_window is resumed by the missing-tab path.
    status.update(key, paused=False, pausing=None, close_warned_at=None, undelivered=None,
                  started_at=ops.now().isoformat())
    ops.new_window(key, s["launch_dir"],
                   _claude(key, f"--resume {s['session_id']} {shlex.quote(RESUMED.format(comment=comment))}"))


def started(s: dict) -> datetime:
    """When the worker was last started or resumed after a pause; for workers started before PMAT-355, its first hook
    (hooks write local time, so compared as times, not strings)."""
    at = s.get("started_at") or next((h["at"] for h in s.get("history", [])), None)
    return datetime.fromisoformat(at) if at else datetime.min.replace(tzinfo=timezone.utc)


def choose_pauses(keys: list[str], cap: int, first: str | None, waiting: bool) -> None:
    """Mark the workers to pause (the caller holds status.queue_lock), so the running workers come down to `cap`
    and, while the priority ticket `first` is `waiting` in the queue, one slot is left for it. Workers at their
    prompt go first, as they can be warned now and free a slot soonest (owner, PMAT-355); among those, and then
    among busy ones, the latest in the ticket order first. Workers already warned (in their pause grace) or closing
    as Done count as running but leaving, so one slot never pauses two. A pause not yet warned (every candidate
    busy) is chosen again each check, so it moves to a worker that reaches its prompt, or is dropped when no longer
    needed (e.g. the cap was raised)."""
    statuses = {k: status.read(k) for k in keys}
    # Workers started before order.txt existed join the order by when they started.
    rank = status.add_to_order(sorted(keys, key=lambda k: started(statuses[k])))
    leaving = [k for k, s in statuses.items()
               if (s.get("pausing") or {}).get("warned_at") or s.get("close_warned_at")]
    eligible = sorted((k for k, s in statuses.items() if k not in leaving and k != first and not s.get("keep_running")),
                      key=lambda k: (statuses[k].get("state") in AT_PROMPT, rank.index(k)), reverse=True)
    staying = len(keys) - len(leaving)
    over = max(staying - cap, 0)
    reasons = [f"cap {cap}"] * over + ([f"room for priority {first}"] if waiting and staying - over >= cap else [])
    chosen = dict(zip(eligible, reasons))
    for k, s in statuses.items():
        pausing = s.get("pausing") or {}
        if pausing.get("warned_at"):
            continue
        if k in chosen and pausing.get("reason") != chosen[k]:
            status.update(k, pausing={"reason": chosen[k], "warned_at": None})
        elif k not in chosen and pausing:
            status.update(k, pausing=None)


def tick(keys: list[str], tickets: dict[str, Ticket], ops: Ops) -> list[str]:
    """One pass over the active tickets; returns what was done, for the log. Each ticket is handled on its
    own, so one ticket's failure (e.g. a tab gone mid-tick) does not starve the others."""
    done, alive = [], ops.windows()
    # Latest in the ticket order first, the order workers are chosen to pause in.
    rank = status.order()
    for key in sorted(keys, key=lambda k: rank.index(k) if k in rank else len(rank), reverse=True):
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


def _pause(key: str, alive: bool, s: dict, t: Ticket | None, ops: Ops) -> str:
    """Close the tab and queue the ticket as paused, keeping its session and folder to resume it."""
    warned = (s.get("pausing") or {}).get("warned_at")
    posted = (t and t.worker_comment and warned
              and datetime.fromisoformat(t.worker_comment[1]) >= datetime.fromisoformat(warned))
    if alive:  # killing a missing tab fails
        ops.kill(key)
    status.update(key, paused=True, pausing=None, pause_comment=t.worker_comment[0] if posted else None,
                  paused_at=ops.now().isoformat(timespec="seconds"), undelivered=None)
    status.queue_paused(key, s["launch_dir"])
    if posted or t is None:  # Jira not read this check: whether it posted is unknown, so no claim either way
        return f"{key}: paused"
    return f"{key}: paused without a Jira comment"


def _tick_pausing(key: str, alive: bool, s: dict, t: Ticket | None, ops: Ops) -> str | None:
    pausing = s["pausing"]
    if not (warned := pausing.get("warned_at")):
        if not alive:  # nothing running to warn: pause it now rather than resume it only to pause it
            return _pause(key, alive, s, t, ops)
        if s.get("state") not in AT_PROMPT:
            return None  # busy: warned once at its prompt (the screen says "pause pending")
        if _gave_up(key, s, "the pause warning"):
            return None
        minutes = CLOSE_GRACE.seconds // 60
        if not (sent := _send_confirmed(key, PAUSING.format(reason=pausing["reason"], minutes=minutes, key=key), ops)):
            return _lost(key, s, "the pause warning", ops)
        status.update(key, pausing=pausing | {"warned_at": sent}, undelivered=None)
        return f"{key}: pausing ({pausing['reason']}), closing in {minutes} min"
    if alive and _prompts_since(s, warned) > 1:  # our warning, then the owner typed: the owner keeps it running
        status.update(key, pausing=None, keep_running=True)
        return f"{key}: pause cancelled (owner typed)"
    if not alive or ops.now() - datetime.fromisoformat(warned) >= CLOSE_GRACE:
        return _pause(key, alive, s, t, ops)
    return None


def _tick_one(key: str, alive: bool, t: Ticket | None, ops: Ops) -> str | None:
    s = status.read(key)
    if s.get("pausing"):  # in its pause grace, nothing else is typed into the tab (it would count as the owner)
        return _tick_pausing(key, alive, s, t, ops)
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
