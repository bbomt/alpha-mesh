"""The team lead: starts a worker (a Claude session in its own tmux tab) per ticket and, each tick, does
what the owner would do by hand in that tab. The worker runs the ticket itself through the Claude harness
(worktree, branch, labels, PR, Done); the orchestrator only reads Jira and never writes it.
"""

import shlex
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from orchestrator import status
from orchestrator.jira import Ticket

AGENT_DIR = Path(__file__).resolve().parents[2]
HOOKS = AGENT_DIR / "smoke/hooks-settings.json"
# How an orchestrated worker differs from a manual session (Jira questions, comment marker, worktree);
# passed on every start and resume, as a system prompt does not survive the process.
WORKER_PROMPT = AGENT_DIR / "worker-prompt.md"

START = "Work on Jira {key}."
NUDGE = ("You were interrupted (the machine or this tab stopped). Check where you were (git status, the "
         "ticket, your last steps) and carry on, without redoing finished work.")
REPLY = "The owner commented on Jira {key} (newest comment {comment}). Read their new comments and act on them."
MERGED = "The PR for Jira {key} is merged. Finish the ticket as CLAUDE.md says."
CLOSING = ("Team lead: Jira {key} is Done, so I will close this tab in {minutes} minutes. To keep it, type anything "
           "here before then. No reply needed.")
CLOSE_GRACE = timedelta(minutes=5)

# Hook states in which the worker sits at its prompt, so typing into the tab is safe.
AT_PROMPT = ("idle", "waiting")


@dataclass
class Ops:
    """The outside world, so tests can fake it."""
    windows: Callable[[], set[str]]
    new_window: Callable[[str, str, str], None]
    send: Callable[[str, str], None]
    kill: Callable[[str], None]
    merged: Callable[[str, str], bool]  # (ticket, repo dir) -> its PR is merged
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)


def _claude(key: str, args: str) -> str:
    return (f"ORCH_TICKET={shlex.quote(key)} claude {args} --settings {shlex.quote(str(HOOKS))} "
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
    own, so one ticket's failure (a tab gone mid-tick, gh offline) does not starve the others."""
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


def _tick_one(key: str, alive: bool, t: Ticket | None, ops: Ops) -> str | None:
    s = status.read(key)
    if not alive:
        # Run from the launch folder: Claude finds a session to resume by the folder it started in.
        ops.new_window(key, s["launch_dir"], _claude(key, f"--resume {s['session_id']} {shlex.quote(NUDGE)}"))
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
    if t.comment_id and not t.comment_from_worker and t.comment_id != s["relayed_comment"]:
        ops.send(key, REPLY.format(key=key, comment=t.comment_id))
        status.update(key, relayed_comment=t.comment_id)
        return f"{key}: passed on the owner's comment {t.comment_id}"
    if "AWAITING-MERGE" in t.labels and not s.get("merge_relayed") and ops.merged(key, s["launch_dir"]):
        ops.send(key, MERGED.format(key=key))
        status.update(key, merge_relayed=True)
        return f"{key}: PR merged, told the worker"
    if t.done and "AWAITING-USER-RESPONSE" not in t.labels:
        # Done at the prompt with no open question: finished, however the merge reached the worker. Warn in
        # the tab first, so an owner working in it can keep it.
        if not (warned := s.get("close_warned_at")):
            now = ops.now().isoformat(timespec="seconds")
            status.update(key, close_warned_at=now)
            ops.send(key, CLOSING.format(key=key, minutes=CLOSE_GRACE.seconds // 60))
            return f"{key}: Done, warned the tab it closes in {CLOSE_GRACE.seconds // 60} minutes"
        if _prompts_since(s, warned) > 1:  # our warning, then the owner typed
            status.update(key, finished=True, kept_by_owner=True)
            return f"{key}: Done, the owner kept the tab; it is theirs now"
        if ops.now() - datetime.fromisoformat(warned) >= CLOSE_GRACE:
            ops.kill(key)
            status.update(key, finished=True)
            return f"{key}: Done, closed its tab"
    return None


def table(keys: list[str], tickets: dict[str, Ticket], ops: Ops) -> str:
    alive = ops.windows()
    rows = [("TICKET", "TAB", "WORKER", "JIRA", "LABELS")]
    for key in keys:
        s, t = status.read(key), tickets.get(key)
        rows.append((key, "open" if key in alive else "missing", s.get("state", "-"),
                     "-" if t is None else ("Done" if t.done else "open"),
                     "-" if t is None else ",".join(t.labels)))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)
