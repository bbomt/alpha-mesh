"""mesh orchestrate [TICKET ...] [--repo DIR] [--interval SECONDS] [--once]

Starts a worker for each new ticket, then loops: resumes any worker whose tab is gone (after a reboot,
just run it again), passes on the owner's Jira comments, closes finished tabs, and redraws a
screen saying what needs the owner.
"""

import argparse
import asyncio
import fcntl
import functools
import json
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone

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
  mesh orchestrate PMAT-282 PMAT-283   start a worker for each ticket, then keep watching
  mesh orchestrate                     pick up the running workers (after a restart or reboot)
  mesh orchestrate --once              one check, then exit

Run it in its own tmux tab, from the repo the workers should start in. Each worker is a Claude session in
a tab named after its ticket; it works the ticket itself, as CLAUDE.md says. Each check, for each ticket:
  - its tab is missing (reboot, closed tab)   reopen the tab and resume the same Claude session
  - you commented on Jira                     type "The owner commented on Jira ..." into the tab
  - the ticket is Done                        warn in the tab, close it {loop.CLOSE_GRACE.seconds // 60} minutes later unless you type there

Talking to workers: answer a worker's question with a Jira comment, or type in its tab. Worker comments
start with MESSAGE-FROM-WORKER; any other comment is taken as yours. Approve a worker's PR the same way
("approved to merge"); the worker merges master in, reruns its tests and merges the PR itself.

Stopping: Ctrl-C stops only the orchestrator; the workers keep running. Starting it again is safe at any
time, and only one copy can run.

Files (in ~/environment/logs/orchestrator/):
  <TICKET>.json   each worker's session, state and what was passed on
  events.log      every event, as shown under Recent

First run: it prints an Atlassian login link; open it in a fresh browser tab and paste back the URL the
browser ends on. Full documentation: agents/orchestrator/README.md
"""

OPS = loop.Ops(windows=tmux.windows, new_window=tmux.new_window, send=tmux.send, kill=tmux.kill,
               background=tmux.background)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mesh orchestrate", description=__doc__.split("\n\n")[1], epilog=HELP,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tickets", nargs="*", help="Jira keys to start, e.g. PMAT-282")
    p.add_argument("--repo", default=".", help="repo the workers start in (default: current folder)")
    p.add_argument("--interval", type=int, default=30, help="seconds between checks (default 30)")
    p.add_argument("--once", action="store_true", help="run one check and exit")
    args = p.parse_args(argv)
    # One team lead at a time: two would both type into the same tabs. The lock goes when the process does.
    status.DIR.mkdir(parents=True, exist_ok=True)
    lock = open(status.DIR / "orchestrator.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("mesh orchestrate is already running (in another tab); not starting a second copy.", file=sys.stderr)
        return 1
    tty = sys.stdout.isatty()
    events: deque[tuple[datetime, str]] = deque(maxlen=10)
    log = status.DIR / "events.log"
    if log.exists():  # a restart (or reboot) keeps the recent history on screen
        for line in log.read_text().splitlines()[-10:]:
            when, _, text = line.partition("  ")
            events.append((datetime.fromisoformat(when), text))

    def event(text: str) -> None:
        now = datetime.now(timezone.utc)
        events.append((now, text))
        status.DIR.mkdir(parents=True, exist_ok=True)
        with open(log, "a") as f:
            f.write(f"{now.isoformat(timespec='seconds')}  {text}\n")

    repo = subprocess.run(["git", "-C", args.repo, "rev-parse", "--show-toplevel"],
                          check=True, capture_output=True, text=True).stdout.strip()
    for key in args.tickets:
        if key in status.active():
            event(f"{key}: already running")
        else:
            loop.start(key, repo, OPS)
            event(f"{key}: started a worker in tab {key}")

    while True:
        keys = status.active()
        try:
            tickets = asyncio.run(jira.snapshot(keys)) if keys else {}
        except Exception as e:  # Jira or network down: keep the workers' tabs alive, try again next tick
            event(f"Jira read failed, trying again next tick: {e!r}")
            tickets = {}
        try:
            found = prs(keys, repo) if keys else {}
        except Exception as e:
            found = {}
            event(f"PR lookup failed, trying again next tick: {e!r}")
        try:
            for line in loop.tick(keys, tickets, OPS):
                event(line)
        except Exception as e:  # a tab closed mid-tick...: the workers run on, so must the lead
            event(f"Tick failed, trying again next tick: {e!r}")
        try:
            draw = functools.partial(display.render, keys, {k: status.read(k) for k in keys}, tickets, found,
                                     OPS.windows(), list(events), datetime.now(timezone.utc),
                                     grace_minutes=loop.CLOSE_GRACE.seconds // 60, color=tty,
                                     background=OPS.background())
        except Exception as e:
            print(f"Screen failed, trying again next tick: {e!r}", file=sys.stderr, flush=True)
            draw = None
        if args.once or not tty:
            if draw:
                print(draw(next_in=args.interval), flush=True)
            if args.once:
                return 0
            time.sleep(args.interval)
            continue
        for left in range(args.interval, 0, -1):  # in a terminal, the header counts down to the next check
            if draw:
                screen = draw(next_in=left).replace("\n", display.CLEAR_LINE + "\n")
                print(display.HOME + screen + display.CLEAR_LINE + display.CLEAR_BELOW, end="", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
