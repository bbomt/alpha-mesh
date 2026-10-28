"""Per-ticket status files, shared with the session's hooks (status-hook.sh).

The hook writes the session's state; the orchestrator adds its own fields (launch_dir, relayed_comment,
close_warned_at, finished). Both take the same lock, so neither loses the other's update.
"""

import fcntl
import json
from pathlib import Path

DIR = Path.home() / "environment/logs/orchestrator"


def path(key: str) -> Path:
    return DIR / f"{key}.json"


def read(key: str) -> dict:
    p = path(key)
    return json.loads(p.read_text()) if p.exists() else {}


def update(key: str, **fields) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    p = path(key)
    with open(f"{p}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(read(key) | fields, indent=2))
        tmp.replace(p)


def active() -> list[str]:
    """Tickets the orchestrator started and has not finished."""
    if not DIR.exists():
        return []
    keys = (p.stem for p in sorted(DIR.glob("*.json")))
    return [k for k in keys if (s := read(k)).get("launch_dir") and not s.get("finished")]


def queued() -> list[tuple[str, str]]:
    """Tickets waiting for a free worker (--max-workers), in order, each with the repo folder it starts in.
    Kept in queue.txt (one "KEY FOLDER" line each), not a .json file, so active() never takes it for a ticket."""
    p = DIR / "queue.txt"
    return [tuple(line.split(" ", 1)) for line in p.read_text().splitlines()] if p.exists() else []


def save_queue(entries: list[tuple[str, str]]) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / "queue.txt.tmp"
    tmp.write_text("".join(f"{key} {folder}\n" for key, folder in entries))
    tmp.replace(DIR / "queue.txt")
