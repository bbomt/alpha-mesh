"""mesh orchestrate [TICKET ...] [--repo DIR] [--interval SECONDS] [--once]

Starts a worker for each new ticket, then loops: resumes any worker whose tab is gone (after a reboot,
just run it again), passes on the owner's Jira comments and merges, closes finished tabs, and redraws a
screen saying what needs the owner.
"""

import argparse
import asyncio
import json
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone

from orchestrator import display, jira, loop, status, tmux


def pr(key: str, repo_dir: str) -> display.Pr | None:
    """The newest PR with the ticket key in its title."""
    out = subprocess.run(["gh", "pr", "list", "--state", "all", "--search", f"{key} in:title",
                          "--json", "number,state,url", "--limit", "1"],
                         cwd=repo_dir, check=True, capture_output=True, text=True).stdout
    found = json.loads(out or "[]")
    return display.Pr(found[0]["number"], found[0]["state"], found[0]["url"]) if found else None


OPS = loop.Ops(windows=tmux.windows, new_window=tmux.new_window, send=tmux.send, kill=tmux.kill)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mesh orchestrate", description=__doc__.split("\n\n")[1])
    p.add_argument("tickets", nargs="*", help="Jira keys to start, e.g. PMAT-282")
    p.add_argument("--repo", default=".", help="repo the workers start in (default: current folder)")
    p.add_argument("--interval", type=int, default=30, help="seconds between ticks (default 30)")
    p.add_argument("--once", action="store_true", help="run one tick and exit")
    args = p.parse_args(argv)
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
        prs = {}
        for key in keys:
            try:
                prs[key] = pr(key, status.read(key)["launch_dir"])
            except Exception as e:
                prs[key] = None
                event(f"{key}: PR lookup failed, trying again next tick: {e!r}")
        try:
            for line in loop.tick(keys, tickets, prs, OPS):
                event(line)
            screen = display.render(keys, {k: status.read(k) for k in keys}, tickets, prs, OPS.windows(),
                                    list(events), datetime.now(timezone.utc), args.interval,
                                    loop.CLOSE_GRACE.seconds // 60, color=tty)
            print((display.CLEAR if tty else "") + screen, flush=True)
        except Exception as e:  # a tab closed mid-tick...: the workers run on, so must the lead
            print(f"Tick failed, trying again next tick: {e!r}", file=sys.stderr, flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
