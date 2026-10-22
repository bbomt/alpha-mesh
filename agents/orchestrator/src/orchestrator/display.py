"""The orchestrator's screen, redrawn in place each tick: one row per worker saying what, if anything, the
owner must do, then the latest events."""

from dataclasses import dataclass
from datetime import datetime

from orchestrator.jira import Ticket

CLEAR = "\033[H\033[2J"
RESET, BOLD, DIM, RED, YELLOW, GREEN = "\033[0m", "\033[1m", "\033[2m", "\033[31m", "\033[33m", "\033[32m"


@dataclass(frozen=True)
class Pr:
    number: int
    state: str  # OPEN, MERGED or CLOSED, as gh reports it


def ago(since: str | None, now: datetime) -> str:
    if not since:
        return "-"
    minutes = int((now - datetime.fromisoformat(since)).total_seconds() // 60)
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"


def needs(s: dict, t: Ticket | None, pr: Pr | None, alive: bool, now: datetime, grace_minutes: int) -> tuple[str, str]:
    """What the owner must do for this worker, and the colour to show it in."""
    if not alive:
        return "resuming the worker", YELLOW
    if t and "AWAITING-USER-RESPONSE" in t.labels:
        return "▶ answer on Jira", BOLD + YELLOW
    if s.get("state") == "waiting" and s.get("notification_type") not in (None, "idle_prompt"):
        return f"▶ check the tab: {s.get('message') or s.get('notification_type')}", BOLD + RED
    if warned := s.get("close_warned_at"):
        left = grace_minutes - int((now - datetime.fromisoformat(warned)).total_seconds() // 60)
        return f"closing in {max(left, 0)}m (type in the tab to keep it)", DIM
    if t and "AWAITING-MERGE" in t.labels:
        return (f"▶ merge PR #{pr.number}" if pr and pr.state == "OPEN" else "▶ merge the PR"), BOLD + YELLOW
    return "", ""


def worker(s: dict, alive: bool) -> tuple[str, str]:
    if not alive:
        return "tab missing", RED
    state = s.get("state", "starting")
    return state, GREEN if state == "working" else DIM


def render(keys: list[str], statuses: dict[str, dict], tickets: dict[str, Ticket], prs: dict[str, Pr | None],
           alive: set[str], events: list[tuple[datetime, str]], now: datetime, interval: int,
           grace_minutes: int, color: bool = True) -> str:
    def paint(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if color and code and text else text

    rows = [(("TICKET", ""), ("WORKER", ""), ("FOR", ""), ("JIRA", ""), ("PR", ""), ("NEEDS YOU", ""))]
    for key in keys:
        s, t, pr = statuses.get(key, {}), tickets.get(key), prs.get(key)
        rows.append(((key, BOLD), worker(s, key in alive), (ago(s.get("updated"), now), ""),
                     ("-" if t is None else "Done" if t.done else "open", ""),
                     ("-" if pr is None else f"#{pr.number} {pr.state.lower()}", ""),
                     needs(s, t, pr, key in alive, now, grace_minutes)))
    widths = [max(len(r[i][0]) for r in rows) for i in range(len(rows[0]))]
    table = ["  ".join(paint(text.ljust(w), code) for (text, code), w in zip(r, widths)).rstrip() for r in rows]
    table[0] = paint(table[0], DIM)

    head = (f"mesh orchestrate · {len(keys)} worker{'s' if len(keys) != 1 else ''} · "
            f"checked {now.astimezone():%H:%M:%S} · next check in {interval}s")
    recent = [f"{when.astimezone():%H:%M:%S}  {text}" for when, text in reversed(events)] or ["(nothing yet)"]
    return "\n".join([paint(head, BOLD), "", *table, "", paint("Recent", DIM), *recent, "",
                      paint("Ctrl-C stops the orchestrator; the workers keep running. "
                            "Run `mesh orchestrate` again to pick them up.", DIM)])
