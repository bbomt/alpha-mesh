"""mesh orchestrate [TICKET ...] [--repo DIR] [--interval SECONDS] [--once]

Starts a worker for each new ticket, then loops: resumes any worker whose tab is gone (after a reboot,
just run it again), passes on the owner's Jira replies and merges, closes finished tabs, prints a table.
"""

import argparse
import asyncio
import subprocess
import sys
import time
from datetime import datetime

from orchestrator import jira, loop, status, tmux


def merged(key: str, repo_dir: str) -> bool:
    out = subprocess.run(["gh", "pr", "list", "--state", "merged", "--search", f"{key} in:title",
                          "--json", "number", "--jq", "length"],
                         cwd=repo_dir, check=True, capture_output=True, text=True).stdout
    return int(out.strip() or 0) > 0


OPS = loop.Ops(windows=tmux.windows, new_window=tmux.new_window, send=tmux.send, kill=tmux.kill, merged=merged)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="mesh orchestrate", description=__doc__.split("\n\n")[1])
    p.add_argument("tickets", nargs="*", help="Jira keys to start, e.g. PMAT-282")
    p.add_argument("--repo", default=".", help="repo the workers start in (default: current folder)")
    p.add_argument("--interval", type=int, default=30, help="seconds between ticks (default 30)")
    p.add_argument("--once", action="store_true", help="run one tick and exit")
    args = p.parse_args(argv)

    repo = subprocess.run(["git", "-C", args.repo, "rev-parse", "--show-toplevel"],
                          check=True, capture_output=True, text=True).stdout.strip()
    for key in args.tickets:
        if key in status.active():
            print(f"{key}: already running")
        else:
            loop.start(key, repo, OPS)
            print(f"{key}: started a worker in tab {key}")

    while True:
        keys = status.active()
        try:
            tickets = asyncio.run(jira.snapshot(keys)) if keys else {}
        except Exception as e:  # Jira or network down: keep the workers' tabs alive, try again next tick
            print(f"Jira read failed: {e}", file=sys.stderr)
            tickets = {}
        try:
            for line in loop.tick(keys, tickets, OPS):
                print(line)
            print(f"\n{datetime.now():%H:%M:%S}\n{loop.table(keys, tickets, OPS)}\n", flush=True)
        except Exception as e:  # a tab closed mid-tick, gh offline...: the workers run on, so must the lead
            print(f"Tick failed, trying again next tick: {e!r}", file=sys.stderr, flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
