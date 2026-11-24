"""mesh orchestrate [TICKET ...] [--repo-dir DIR] [--max-workers N] [--priority TICKET] [--interval SECONDS]

Starts a worker for each new ticket (up to --max-workers at once; the rest wait in a queue), then loops:
resumes any worker whose tab is gone (after a reboot, just run it again), passes on the owner's Jira comments,
closes finished tabs, starts queued tickets, and redraws a screen saying what needs the owner.
"""

import argparse
import asyncio
import fcntl
import functools
import json
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from orchestrator import display, jira, loop, status, tmux


def prs(keys: list[str], repo_dir: str) -> dict[str, display.Pr | None]:
    """Each ticket's newest PR, found by its key in brackets in the title (`feature[PMAT-282]: ...`; a title
    may mention other tickets, e.g. "for PMAT-281"). Lists the repo's recent PRs in one call:
    gh's --search goes through GitHub search, which returned nothing for about half of identical calls."""
    out = subprocess.run(["gh", "pr", "list", "--state", "all", "--limit", "100",
                          "--json", "number,state,url,title"],
                         cwd=repo_dir, check=True, capture_output=True, text=True).stdout
    found = json.loads(out or "[]")  # newest first
    return {key: next((display.Pr(p["number"], p["state"], p["url"]) for p in found
                       if f"[{key}]" in p["title"]), None) for key in keys}


HELP = f"""examples:
  mesh orchestrate --repo-dir ~/environment/pmat/alpha-mesh PMAT-282 PMAT-283
                                       start a worker for each ticket in that repo, then keep watching
  mesh orchestrate                     pick up the running workers (after a restart or reboot)
  mesh orchestrate --max-workers 1 --priority PMAT-290
                                       work on PMAT-290 alone: the other workers are paused (also while running)

Run it in its own tmux tab. Tickets named on the command line start in --repo-dir, the local folder of
their repo (required with tickets). Running tickets keep their own folder, so one orchestrator can run
tickets from several repos: mesh orchestrate --repo-dir ~/environment/pmat/<other-repo> KEY ... (also while it runs)
Each worker first checks that its ticket belongs to the repo it was started in. Each worker is a Claude session in
a tab named after its ticket; it works the ticket itself, as CLAUDE.md says. Each check, for each ticket:
  - its tab is missing (reboot, closed tab)   reopen the tab and resume the same Claude session
  - you commented on Jira                     type "The owner commented on Jira ..." into the tab
  - the ticket is Done                        warn in the tab, close it {loop.CLOSE_GRACE.seconds // 60} minutes later unless you type there
No more than --max-workers workers run at once (kept until changed; default 3). Further tickets wait in the Queue
box, in command-line order; each check starts the next one when a worker has finished. Resumed workers count
toward the cap.

While it runs, mesh orchestrate in another shell changes it from its next check: --max-workers N sets the cap,
TICKET ... --repo-dir DIR queues tickets, --priority TICKET sets the priority ticket. With none of these, a second
copy is refused.

Ticket order: tickets keep the order they were added in; only --priority puts one first, and pausing never
moves one. Pausing: while more workers run than the cap, or the --priority ticket waits with every slot taken,
the worker latest in that order (never the priority ticket) is paused. Once at its prompt, it is told to stop its
processes and post its status on Jira; its tab closes {loop.CLOSE_GRACE.seconds // 60} minutes later unless you type there (then it is never
paused again). It waits in the Queue marked paused, in its own place, and resumes its same Claude session
when a slot frees up. --priority TICKET (one at a time) moves a ticket to the front; a new one needs
--repo-dir. A ticket that loses the mark to a newer one is an ordinary worker again (it may be paused for room).

Talking to workers: answer a worker's question with a Jira comment, or type in its tab. Worker comments
start with MESSAGE-FROM-WORKER; any other comment is taken as yours. Merge a worker's PR yourself (by hand,
or with the mesh merge-pr command its "ready to merge" comment ends with), then tell it the same way.

Stopping: Ctrl-C stops only the orchestrator; the workers keep running. Starting it again is safe at any
time, and only one copy can run.

Files (in ~/environment/logs/orchestrator/):
  <TICKET>.json   each worker's session, state and what was passed on
  queue.txt       tickets waiting for a free worker, in order
  cap.txt         the cap (--max-workers);  priority.txt  the priority ticket (--priority)
  order.txt       every ticket, in the order it was added
  events.log      every event, as shown under Recent

First run: it prints an Atlassian login link; open it in a fresh browser tab and paste back the URL the
browser ends on. Full documentation: agents/orchestrator/README.md
"""

def disk_used() -> float:
    """The fraction of the disk holding the home folder (worktrees, caches, logs) that is in use."""
    u = shutil.disk_usage(Path.home())
    return u.used / u.total


def prune(folder: str) -> None:
    subprocess.run(["git", "-C", folder, "worktree", "prune"], check=True, capture_output=True)


OPS = loop.Ops(windows=tmux.windows, new_window=tmux.new_window, send=tmux.send, kill=tmux.kill,
               background=tmux.background, prune=prune)


# Seconds one Jira check may take. An MCP call can wait for a reply for ever, which froze the loop (PMAT-306).
JIRA_DEADLINE = 120


async def check(keys: list[str], deadline: float | None, show=None) -> dict:
    """The Jira snapshot, cut off at `deadline`. `show(elapsed)` (if given) is called every second meanwhile, and
    with the full deadline on a timeout, so the owner sees the check is running rather than a frozen screen."""
    start = time.monotonic()

    async def redraw():
        while True:
            show(time.monotonic() - start)
            await asyncio.sleep(1)

    ticker = asyncio.create_task(redraw()) if show else None
    try:
        return await asyncio.wait_for(jira.snapshot(keys), deadline)
    except TimeoutError:
        if show:
            show(deadline)
        raise
    finally:
        if ticker:
            ticker.cancel()


def paint(screen: str) -> None:
    """Draw a screen over the last one, in place."""
    screen = screen.replace("\n", display.CLEAR_LINE + "\n")
    print(display.HOME + screen + display.CLEAR_LINE + display.CLEAR_BELOW, end="", flush=True)


# A key is a tab name, part of the shell command tmux runs, and a status file name, so it must be a plain Jira key.
KEY = re.compile(r"[A-Z][A-Z0-9]*-[0-9]+")


def settings(args, repo: str | None, event, p: argparse.ArgumentParser) -> list[str]:
    """Apply --max-workers, the tickets named and --priority to the stored settings and queue (the caller holds
    status.queue_lock), whether this copy goes on to run the loop or one is already running. Returns what changed."""
    said = []

    def say(text: str) -> None:
        said.append(text)
        event(text)

    if args.max_workers is not None:
        status.save_cap(args.max_workers)
        say(f"cap set to {args.max_workers}")
    queue, active = status.queued(), status.active()
    # The ticket order (order.txt): running tickets were added first, then the queue, then the tickets named here.
    status.add_to_order([*sorted(active, key=lambda k: loop.started(status.read(k))), *(k for k, _ in queue)])
    for key in args.tickets:
        if key in active:
            say(f"{key}: already running")
        elif key in (k for k, _ in queue):
            say(f"{key}: already queued")
        else:
            queue.append((key, repo))
            said.append(f"{key}: queued")
    if (key := args.priority) and key != (old := status.priority()):
        if key not in active:
            entry = next((e for e in queue if e[0] == key), None)
            if entry is None and repo is None:  # never start a ticket in whatever folder the owner is in
                p.error(f"--repo-dir is required to make a new ticket the priority: the local folder of {key}'s repo")
            queue = [entry or (key, repo), *(e for e in queue if e[0] != key)]
        # The old one (if any) is an ordinary ticket from now on, so it may be paused to make room for this one.
        if status.path(key).exists():
            status.update(key, pausing=None)  # the priority ticket is never paused: drop its pause
        status.save_priority(key)
        say(f"{key}: priority, already running" if key in active
            else f"{key}: priority, moved to the front of the queue")
        said.append(f"{key} is the priority ticket")
    elif key:
        said.append(f"{key} is already the priority ticket; nothing changed")
    status.save_queue(status.in_order(queue))
    return said


def main(argv: list[str] | None = None, once: bool = False) -> int:
    """`once` runs a single check and returns; tests use it, it isn't a command-line option."""
    p = argparse.ArgumentParser(prog="mesh orchestrate", description=__doc__.split("\n\n")[1], epilog=HELP,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tickets", nargs="*", help="Jira keys to start, e.g. PMAT-282")
    p.add_argument("--repo-dir", metavar="DIR",
                   help="local folder of the repo (its root) where the tickets named here start; required with tickets")
    p.add_argument("--max-workers", type=int, metavar="N",
                   help="most workers running at once; workers above it are paused, further tickets wait in a "
                        f"queue. Kept until changed (default {status.DEFAULT_CAP})")
    p.add_argument("--priority", metavar="TICKET",
                   help="the one priority ticket: to the front of the queue, starting at the next check (the most "
                        "recently started worker is paused to make room); a new ticket needs --repo-dir")
    p.add_argument("--interval", type=int, default=30, help="seconds between checks (default 30)")
    args = p.parse_args(argv)
    if args.tickets and not args.repo_dir:  # never start a ticket in whatever folder the owner happens to be in
        p.error("--repo-dir is required when starting tickets: the local folder of their repo")
    if bad := [k for k in args.tickets if not KEY.fullmatch(k)]:
        p.error(f"not a Jira key (expected e.g. PMAT-282): {', '.join(bad)}")
    if args.priority and not KEY.fullmatch(args.priority):
        p.error(f"not a Jira key (expected e.g. PMAT-282): {args.priority}")
    if args.max_workers is not None and args.max_workers < 1:
        p.error("--max-workers must be at least 1")
    # One mesh orchestrator at a time: two would both type into the same tabs. The lock goes when the process does.
    status.DIR.mkdir(parents=True, exist_ok=True)
    lock = open(status.DIR / "orchestrator.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        running_elsewhere = False
    except BlockingIOError:
        running_elsewhere = True
        if not (args.tickets or args.priority or args.max_workers is not None):
            print("mesh orchestrate is already running (in another tab); not starting a second copy.",
                  file=sys.stderr)
            return 1
    tty = sys.stdout.isatty()
    log = status.DIR / "events.log"

    def recent() -> list[tuple[datetime, str]]:
        """The last 10 events, from the log: a restart keeps them, and a second copy's settings change shows too."""
        lines = log.read_text().splitlines()[-10:] if log.exists() else []
        return [(datetime.fromisoformat(when), text) for when, _, text in (line.partition("  ") for line in lines)]

    def event(text: str) -> None:
        now = datetime.now(timezone.utc)
        status.DIR.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as f:
            f.write(f"{now.isoformat(timespec='seconds')}  {text}\n")

    repo = None
    if args.tickets or args.repo_dir:
        repo = subprocess.run(["git", "-C", args.repo_dir, "rev-parse", "--show-toplevel"],
                              check=True, capture_output=True, text=True).stdout.strip()
    with status.queue_lock():  # a running orchestrator may be starting or pausing a ticket meanwhile
        said = settings(args, repo, event, p)
    if running_elsewhere:  # PMAT-355: the settings are files, so the running copy takes them up
        print("\n".join([*said, "The running orchestrator uses it from its next check."]))
        return 0

    def start_queued() -> None:
        """Start queued tickets while fewer than the cap run (finished and paused workers have left active()).
        A paused ticket resumes its own session; any other starts afresh."""
        with status.queue_lock():
            queue, running, cap = status.queued(), len(status.active()), status.cap()
            while queue and running < cap:
                key, folder = queue.pop(0)
                # First: loop.start saves the ticket's status (so it is active, and resumed if its tab is missing)
                # before opening the tab, so it must not stay queued too, or it would start twice.
                status.save_queue(queue)
                paused = status.read(key).get("paused")
                try:
                    loop.resume_paused(key, OPS) if paused else loop.start(key, folder, OPS)
                except Exception as e:  # e.g. tmux refused the tab: the running workers still need watching
                    if not paused:
                        # No tab, so no session to resume: undo the start and put it back first in line, afresh.
                        status.update(key, launch_dir=None)
                        status.save_queue([(key, folder), *queue])
                    # A paused one is active again, so the missing-tab path resumes its session next check.
                    event(f"Starting a queued ticket failed, trying again next check: {e!r}")
                    return
                event(f"{key}: resumed after pause" if paused else f"{key}: started a worker in tab {key}")
                running += 1

    def choose_pauses() -> None:
        """Pause workers above the cap, or to make room for the priority ticket; the ticks warn and close them."""
        with status.queue_lock():
            first, queued = status.priority(), [k for k, _ in status.queued()]
            keys = status.active()
            if first and first not in keys and first not in queued:  # it finished: no priority ticket now
                status.save_priority(first := None)
            loop.choose_pauses(keys, status.cap(), first, first in queued)

    start_queued()
    for key, _ in status.queued():
        if key in args.tickets:
            event(f"{key}: queued, {len(status.active())} workers already running")

    draw = None  # the last screen drawn: on later checks the Jira check's bar goes in its header
    while True:
        start_queued()
        choose_pauses()
        keys = status.active()
        try:
            # No deadline while logging in: the owner is pasting the redirect URL, so no bar over the prompt.
            deadline = JIRA_DEADLINE if jira.logged_in() else None
            show = None
            if keys and deadline:
                if tty:
                    def show(elapsed, draw=draw, deadline=deadline):
                        bar = display.checking(elapsed, deadline)
                        paint(draw(next_in=None, checking=bar) if draw else bar)
                else:  # piped to a log: one line, as redrawing a bar would fill it
                    print(f"Checking Jira… (up to {deadline // 60} min)", flush=True)
            tickets = asyncio.run(check(keys, deadline, show)) if keys else {}
        except TimeoutError:  # an MCP call that never answers (PMAT-306): same as Jira down
            event("Jira check timed out, retrying next check")
            tickets = {}
        except Exception as e:  # Jira or network down: keep the workers' tabs alive, try again next tick
            event(f"Jira read failed, trying again next tick: {e!r}")
            tickets = {}
        found = {}
        by_dir: dict[str, list[str]] = {}  # each ticket's PR is in the repo its worker started in
        for key in keys:
            if folder := status.read(key).get("launch_dir"):
                by_dir.setdefault(folder, []).append(key)
        for folder, in_folder in by_dir.items():
            try:
                found |= prs(in_folder, folder)
            except Exception as e:
                event(f"PR lookup failed in {folder}, trying again next tick: {e!r}")
        try:
            for line in loop.tick(keys, tickets, OPS):
                event(line)
        except Exception as e:  # a tab closed mid-tick...: the workers run on, so must the orchestrator
            event(f"Tick failed, trying again next tick: {e!r}")
        try:
            keys = status.active()  # again: this check may have closed a finished ticket's tab
            draw = functools.partial(display.render, keys, {k: status.read(k) for k in keys}, tickets, found,
                                     OPS.windows(), recent(), datetime.now(timezone.utc),
                                     grace_minutes=loop.CLOSE_GRACE.seconds // 60, color=tty,
                                     background=OPS.background(), disk=disk_used(),
                                     queued=[k for k, _ in status.queued()], cap=status.cap(),
                                     priority=status.priority(),
                                     paused={k for k, _ in status.queued() if status.read(k).get("paused")})
        except Exception as e:
            print(f"Screen failed, trying again next tick: {e!r}", file=sys.stderr, flush=True)
            draw = None
        if once or not tty:
            if draw:
                print(draw(next_in=args.interval), flush=True)
            if once:
                return 0
            time.sleep(args.interval)
            continue
        for left in range(args.interval, 0, -1):  # in a terminal, the header counts down to the next check
            if draw:
                paint(draw(next_in=left))
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
