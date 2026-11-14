"""The orchestrator's screen, redrawn in place each tick: one row per worker saying what, if anything, the
owner must do, then the latest events."""

from dataclasses import dataclass
from datetime import datetime

from orchestrator.jira import Ticket
from orchestrator.loop import MAX_ATTEMPTS

# Redraw over the old screen (cursor home, clear each line's rest and below) rather than clearing it: no flicker.
HOME, CLEAR_LINE, CLEAR_BELOW = "\033[H", "\033[K", "\033[J"
# Every failure event says "failed" (Jira read, PR lookup, a ticket's tick, the whole tick): shown red, the rest dim.
EVENT_WIDTH = 110  # longer events (e.g. an error's repr) are cut, so the boxes stay one screen wide
RESET, BOLD, DIM, RED, YELLOW, GREEN, BLUE = (
    "\033[0m", "\033[1m", "\033[2m", "\033[31m", "\033[33m", "\033[32m", "\033[34m")


@dataclass(frozen=True)
class Pr:
    number: int
    state: str  # OPEN, MERGED or CLOSED, as gh reports it
    url: str = ""


def ago(since: str | None, now: datetime) -> str:
    if not since:
        return "-"
    minutes = int((now - datetime.fromisoformat(since)).total_seconds() // 60)
    return f"{minutes}m" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}m"


def needs(s: dict, t: Ticket | None, pr: Pr | None, alive: bool, now: datetime, grace_minutes: int,
          busy: bool = False) -> tuple[str, str]:
    """What the owner must do for this worker, and the colour to show it in."""
    if not alive:
        return "resuming the worker", YELLOW
    if busy:  # working, or its background command runs: not waiting on the owner (it may be about to push)
        return "", ""
    if s.get("jira_lost"):  # set by the worker, cleared by its hook on the next working call (jira-access.sh)
        # A reconnect is not a turn, so the worker sits idle until typed to.
        return (f'▶ Jira lost: in tab {s.get("ticket")} run /mcp (may need a browser login), '
                f'then type "continue"'), BOLD + RED
    if (u := s.get("undelivered")) and u["attempts"] >= MAX_ATTEMPTS:  # typed, but the tab never received it
        return f"▶ {u['what']} not delivered: check the tab", BOLD + RED
    if t and "AWAITING-USER-RESPONSE" in t.labels:
        return "▶ answer on Jira", BOLD + YELLOW
    if s.get("state") == "waiting" and s.get("notification_type") not in (None, "idle_prompt"):
        return f"▶ check the tab: {s.get('message') or s.get('notification_type')}", BOLD + RED
    if warned := s.get("close_warned_at"):
        left = grace_minutes - int((now - datetime.fromisoformat(warned)).total_seconds() // 60)
        return f"closing in {max(left, 0)}m (type in the tab to keep it)", DIM
    if t and "AWAITING-MERGE" in t.labels:
        if pr and pr.state == "MERGED":  # merged by hand: nothing tells the worker but the owner
            return f"▶ tell the worker PR #{pr.number} is merged", BOLD + YELLOW
        return (f"▶ merge PR #{pr.number}" if pr and pr.state == "OPEN" else "▶ merge the PR"), BOLD + YELLOW
    return "", ""


def worker(s: dict, alive: bool, background: bool = False) -> tuple[str, str]:
    if not alive:
        return "tab missing", RED
    state = s.get("state", "starting")
    if state == "waiting" and s.get("notification_type") == "idle_prompt":  # a minute at the prompt, not a question
        state = "idle"
    if state == "idle" and background:  # its turn ended, but a background command it started is still running
        state = "working"
    return state, GREEN if state == "working" else DIM


DISK_FULL = 0.90
BAR_WIDTH = 24


def checking(elapsed: float, deadline: float) -> str:
    """A one-line bar filling over the Jira check's deadline, e.g. `Checking Jira  [████░░…]  0:48 / 2:00`."""
    elapsed = min(elapsed, deadline)
    n = round(BAR_WIDTH * elapsed / deadline)

    def clock(seconds: float) -> str:
        return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"

    return f"Checking Jira  [{'█' * n}{'░' * (BAR_WIDTH - n)}]  {clock(elapsed)} / {clock(deadline)}"


def render(keys: list[str], statuses: dict[str, dict], tickets: dict[str, Ticket], prs: dict[str, Pr | None],
           alive: set[str], events: list[tuple[datetime, str]], now: datetime, next_in: int | None,
           grace_minutes: int, color: bool = True, background: set[str] = frozenset(),
           disk: float | None = None, queued: list[str] = (), checking: str = "") -> str:
    """`checking`, while a Jira check runs, replaces the countdown in the header."""
    def paint(text: str, code: str) -> str:
        return f"{code}{text}{RESET}" if color and code and text else text

    rows = [(("TICKET", ""), ("WORKER", ""), ("FOR", ""), ("JIRA", ""), ("PR", ""), ("NEEDS YOU", ""))]
    for key in keys:
        s, t, pr = statuses.get(key, {}), tickets.get(key), prs.get(key)
        shown = worker(s, key in alive, key in background)
        rows.append(((key, YELLOW), shown, (ago(s.get("updated"), now), ""),
                     ("-", "") if t is None else ("Done", GREEN) if t.done else ("open", ""),
                     ("-", "") if pr is None else (f"{pr.url or '#' + str(pr.number)} {pr.state.lower()}", BLUE),
                     needs(s, t, pr, key in alive, now, grace_minutes, busy=shown[0] == "working")))
    widths = [max(len(r[i][0]) for r in rows) for i in range(len(rows[0]))]
    # Each line is kept as (plain, painted): the plain text sets the widths, as colour codes take no space.
    table = [("  ".join(text.ljust(w) for (text, _), w in zip(r, widths)),
              "  ".join(paint(text.ljust(w), code) for (text, code), w in zip(r, widths))) for r in rows]
    table[0] = (table[0][0], paint(table[0][0], DIM))

    head = (f"{len(keys)} worker{'s' if len(keys) != 1 else ''} · checked {now.astimezone():%H:%M:%S} · "
            f"{checking or f'next check in {next_in}s'}")
    full = disk is not None and disk >= DISK_FULL
    if disk is not None:  # the fraction of the home disk in use; red when nearly full, before installs and tests fail
        head += f" · disk {round(disk * 100)}%" + (" nearly full" if full else "")
    recent = [f"{when.astimezone():%H:%M:%S}  {text[:EVENT_WIDTH]}" for when, text in reversed(events)]
    sections = [("mesh orchestrate", [(head, paint(head, RED if full else DIM))]),
                ("Workers", table if keys else [("(none)", paint("(none)", DIM))]),
                *([("Queue", [(line, line) for line in (f"{i}. {k}" for i, k in enumerate(queued, 1))])] if queued else []),
                ("Recent", [(r, paint(r, RED if "failed" in r else DIM)) for r in recent] or [("(nothing yet)", paint("(nothing yet)", DIM))])]
    inner = max(len(plain) for _, lines in sections for plain, _ in lines)
    inner = max(inner, *(len(title) + 2 for title, _ in sections))
    if keys:  # a rule under the column headings, the full width of the box
        sections[1] = ("Workers", [table[0], ("─" * inner, paint("─" * inner, DIM)), *table[1:]])
    out = []
    for title, lines in sections:
        out.append(paint(f"┌─ ", DIM) + paint(title, BOLD) + paint(f" {'─' * (inner - 1 - len(title))}┐", DIM))
        out += [paint("│ ", DIM) + painted + " " * (inner - len(plain)) + paint(" │", DIM) for plain, painted in lines]
        out.append(paint(f"└{'─' * (inner + 2)}┘", DIM))
    out.append(paint("Ctrl-C stops the orchestrator; the workers keep running. "
                     "Run `mesh orchestrate` again to pick them up.", DIM))
    return "\n".join(out)
