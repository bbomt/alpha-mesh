"""mesh orchestrate [TICKET ...] [--repo-dir DIR] [--interval SECONDS]

Starts a worker for each new ticket, then loops: resumes any worker whose tab is gone (after a reboot,
just run it again), passes on the owner's Jira comments, closes finished tabs, and redraws a
screen saying what needs the owner.
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
from collections import deque
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

Run it in its own tmux tab. Tickets named on the command line start in --repo-dir, the local folder of
their repo (required with tickets). Running tickets keep their own folder, so one orchestrator can run
tickets from several repos: Ctrl-C, then mesh orchestrate --repo-dir ~/environment/pmat/<other-repo> KEY ...
Each worker first checks that its ticket belongs to the repo it was started in. Each worker is a Claude session in
a tab named after its ticket; it works the ticket itself, as CLAUDE.md says. Each check, for each ticket:
  - its tab is missing (reboot, closed tab)   reopen the tab and resume the same Claude session
  - you commented on Jira                     type "The owner commented on Jira ..." into the tab
  - the ticket is Done                        warn in the tab, close it {loop.CLOSE_GRACE.seconds // 60} minutes later unless you type there

Talking to workers: answer a worker's question with a Jira comment, or type in its tab. Worker comments
start with MESSAGE-FROM-WORKER; any other comment is taken as yours. Merge a worker's PR yourself (by hand,
or with the mesh merge-pr command its "ready to merge" comment ends with), then tell it the same way.

Stopping: Ctrl-C stops only the orchestrator; the workers keep running. Starting it again is safe at any
time, and only one copy can run.

Files (in ~/environment/logs/orchestrator/):
  <TICKET>.json   each worker's session, state and what was passed on
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


# A key is a tab name, part of the shell command tmux runs, and a status file name, so it must be a plain Jira key.
KEY = re.compile(r"[A-Z][A-Z0-9]*-[0-9]+")


def main(argv: list[str] | None = None, once: bool = False) -> int:
    """`once` runs a single check and returns; tests use it, it isn't a command-line option."""
    p = argparse.ArgumentParser(prog="mesh orchestrate", description=__doc__.split("\n\n")[1], epilog=HELP,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("tickets", nargs="*", help="Jira keys to start, e.g. PMAT-282")
    p.add_argument("--repo-dir", metavar="DIR",
                   help="local folder of the repo (its root) where the tickets named here start; required with tickets")
    p.add_argument("--interval", type=int, default=30, help="seconds between checks (default 30)")
    args = p.parse_args(argv)
    if args.tickets and not args.repo_dir:  # never start a ticket in whatever folder the owner happens to be in
        p.error("--repo-dir is required when starting tickets: the local folder of their repo")
    if bad := [k for k in args.tickets if not KEY.fullmatch(k)]:
        p.error(f"not a Jira key (expected e.g. PMAT-282): {', '.join(bad)}")
    # One mesh orchestrator at a time: two would both type into the same tabs. The lock goes when the process does.
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

    if args.tickets:
        repo = subprocess.run(["git", "-C", args.repo_dir, "rev-parse", "--show-toplevel"],
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
                                     OPS.windows(), list(events), datetime.now(timezone.utc),
                                     grace_minutes=loop.CLOSE_GRACE.seconds // 60, color=tty,
                                     background=OPS.background(), disk=disk_used())
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
                screen = draw(next_in=left).replace("\n", display.CLEAR_LINE + "\n")
                print(display.HOME + screen + display.CLEAR_LINE + display.CLEAR_BELOW, end="", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    sys.exit(main())
