"""The tmux tabs (tmux windows) of the session the orchestrator runs in, one per ticket, named by ticket key."""

import subprocess


def _tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], check=True, capture_output=True, text=True).stdout


def windows() -> set[str]:
    return set(_tmux("list-windows", "-F", "#{window_name}").splitlines())  # names may contain spaces


def background() -> set[str]:
    """Tabs whose process (claude) has a child shell: a background command that outlives Claude's turn. Claude
    runs each command as a child `bash`, or `bwrap` when sandboxed; its other child, `socat`, is the sandbox proxy."""
    panes = dict(line.split("\t") for line in _tmux("list-panes", "-s", "-F", "#{pane_pid}\t#{window_name}")
                 .splitlines())
    ps = subprocess.run(["ps", "-eo", "ppid=,comm="], check=True, capture_output=True, text=True).stdout
    return {panes[ppid] for ppid, comm in (line.split(None, 1) for line in ps.splitlines())
            if ppid in panes and comm.strip() in ("bash", "bwrap")}


def new_window(name: str, cwd: str, command: str) -> None:
    _tmux("new-window", "-d", "-n", name, "-c", cwd, command)


def send(name: str, text: str) -> None:
    """Type a prompt into the tab, as the owner would, then press Enter."""
    _tmux("send-keys", "-t", f":={name}", "-l", text)
    _tmux("send-keys", "-t", f":={name}", "Enter")


def kill(name: str) -> None:
    _tmux("kill-window", "-t", f":={name}")
