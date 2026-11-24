"""Per-ticket status files, shared with the session's hooks (status-hook.sh).

The hook writes the session's state; the orchestrator adds its own fields (launch_dir, relayed_comment,
close_warned_at, finished, started_at, pausing, paused...). Both take the same lock, so neither loses the other's update.

The orchestrator's own settings sit beside them as .txt files (so active() never takes one for a ticket): cap.txt
(--max-workers) and priority.txt (--priority). A second `mesh orchestrate` writes them while the first runs, so
every read-modify-write of the queue and the priority holds queue.lock.
"""

import fcntl
import json
from contextlib import contextmanager
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
    return [k for k in keys if (s := read(k)).get("launch_dir") and not s.get("finished") and not s.get("paused")]


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


@contextmanager
def queue_lock():
    """Held for each read-modify-write of queue.txt and priority.txt, by the running orchestrator and by a second
    `mesh orchestrate` changing its settings, so neither loses the other's change."""
    DIR.mkdir(parents=True, exist_ok=True)
    with open(DIR / "queue.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


DEFAULT_CAP = 3


def cap() -> int:
    """The most workers running at once: the last --max-workers given, kept across restarts."""
    p = DIR / "cap.txt"
    return int(p.read_text()) if p.exists() else DEFAULT_CAP


def save_cap(n: int) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / "cap.txt.tmp"
    tmp.write_text(f"{n}\n")
    tmp.replace(DIR / "cap.txt")


def priority() -> str | None:
    """The one priority ticket (--priority), or None."""
    p = DIR / "priority.txt"
    return (p.read_text().strip() or None) if p.exists() else None


def save_priority(key: str | None) -> None:
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = DIR / "priority.txt.tmp"
    tmp.write_text(f"{key or ''}\n")
    tmp.replace(DIR / "priority.txt")


def queue_paused(key: str, folder: str) -> None:
    """Queue a paused ticket: behind the priority ticket and the tickets paused before it, ahead of the tickets
    that never started."""
    with queue_lock():
        queue, first = queued(), priority()
        at = next((i for i, (k, _) in enumerate(queue) if k != first and not read(k).get("paused")), len(queue))
        save_queue([*queue[:at], (key, folder), *queue[at:]])
