"""The tmux tabs (tmux windows) of the session the orchestrator runs in, one per ticket, named by ticket key."""

import subprocess


def _tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], check=True, capture_output=True, text=True).stdout


def windows() -> set[str]:
    return set(_tmux("list-windows", "-F", "#{window_name}").split())


def new_window(name: str, cwd: str, command: str) -> None:
    _tmux("new-window", "-d", "-n", name, "-c", cwd, command)


def send(name: str, text: str) -> None:
    """Type a prompt into the tab, as the owner would, then press Enter."""
    _tmux("send-keys", "-t", f":={name}", "-l", text)
    _tmux("send-keys", "-t", f":={name}", "Enter")


def kill(name: str) -> None:
    _tmux("kill-window", "-t", f":={name}")
