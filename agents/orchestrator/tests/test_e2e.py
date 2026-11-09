"""End to end: the real `mesh orchestrate` loop (cli.main, one check per call, so each check is also a
restart) against real tmux on its own server, a stand-in `claude` that runs the real status hook, and fake
Jira and gh. The owner's tmux and Jira are never touched."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from orchestrator import cli, jira, loop, status
from orchestrator import tmux as tmux_mod
from orchestrator.jira import Ticket

HERE = Path(__file__).parent
HOOK = HERE.parent / "status-hook.sh"
SERVER = f"orch-e2e-{os.getpid()}"

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")


def tmux(*args: str) -> str:
    return subprocess.run(["tmux", "-L", SERVER, *args], check=True, capture_output=True, text=True).stdout


def wait_for(check, what: str, timeout: float = 10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if result := check():
            return result
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def world(tmp_path, monkeypatch):
    home, stubs, repo, record = tmp_path / "home", tmp_path / "bin", tmp_path / "repo", tmp_path / "record"
    for d in (home, stubs, repo, record):
        d.mkdir()
    shutil.copy(HERE / "claude-stub", stubs / "claude")
    (stubs / "claude").chmod(0o755)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    # A tab's command gets the environment of the process that opens it (the orchestrator), so these go into
    # this process: the stub first on PATH, a throwaway HOME (where the real hook writes the status files), and
    # where the stub records what it saw. The server ignores the owner's tmux config (-f /dev/null).
    for name, value in {"PATH": f"{stubs}:{os.environ['PATH']}", "HOME": str(home),
                        "E2E_DIR": str(record), "E2E_HOOK": str(HOOK)}.items():
        monkeypatch.setenv(name, value)
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    subprocess.run(["tmux", "-L", SERVER, "-f", "/dev/null", "new-session", "-d", "-s", "e2e", "-n", "orchestrator"],
                   check=True, env=env)
    socket = tmux("display-message", "-p", "#{socket_path}").strip()
    pid = tmux("display-message", "-p", "#{pid}").strip()
    monkeypatch.setenv("TMUX", f"{socket},{pid},0")  # the orchestrator's tmux calls now go to this server
    monkeypatch.setattr(status, "DIR", home / "environment/logs/orchestrator")
    monkeypatch.setattr(cli, "prs", lambda keys, repo_dir: {})

    tickets: dict[str, Ticket] = {}

    async def snapshot(keys):
        return {k: tickets[k] for k in keys if k in tickets}

    monkeypatch.setattr(jira, "snapshot", snapshot)
    yield {"repo": str(repo), "record": record, "tickets": tickets}
    subprocess.run(["tmux", "-L", SERVER, "kill-server"], capture_output=True)
    # kill-server returns before the server has gone, and tmux leaves the socket file behind.
    wait_for(lambda: subprocess.run(["tmux", "-L", SERVER, "ls"], capture_output=True).returncode != 0,
             "the test tmux server to exit")
    Path(socket).unlink(missing_ok=True)


def tabs() -> set[str]:
    return set(tmux("list-windows", "-F", "#{window_name}").splitlines())


def lines(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def at_prompt(key: str):
    return lambda: status.read(key).get("state") == "idle"


def turns(key: str) -> int:
    """Finished turns (Stop hooks). Waiting for a new one, not just for `idle`, avoids reading the idle state
    from before a typed prompt was picked up."""
    return sum(1 for h in status.read(key).get("history", []) if h["event"] == "Stop")


def turn_done(key: str, before: int):
    return lambda: turns(key) > before and status.read(key).get("state") == "idle"


def check(world) -> None:
    assert cli.main(["--repo-dir", world["repo"]], once=True) == 0


def fire_hook(key: str, state: str, **hook_input: str) -> None:
    """Fires the real hook directly, for what the stub's tab doesn't do by itself."""
    hook_input = {"session_id": status.read(key)["session_id"], "cwd": "/wt", **hook_input}
    subprocess.run([str(HOOK), state], input=json.dumps(hook_input), text=True, check=True,
                   env={**os.environ, "ORCH_TICKET": key})


def test_a_tab_whose_process_has_a_shell_child_is_running_a_background_task(world):
    # Claude runs a background command as a child shell (bash, or bwrap when sandboxed) that outlives the turn.
    tmux("new-window", "-d", "-n", "PMAT-2", "bash -c 'bash -c \"sleep 30; true\" & wait'")
    tmux("new-window", "-d", "-n", "PMAT-3", "sleep 30")
    assert wait_for(lambda: tmux_mod.background() & {"PMAT-2", "PMAT-3"}, "the child shell to start") == {"PMAT-2"}


@pytest.mark.parametrize("before", ["idle", "waiting"])
def test_a_compact_at_the_prompt_changes_nothing_and_the_next_comment_still_arrives(world, before):
    record, tickets = world["record"], world["tickets"]
    tickets["PMAT-1"] = Ticket(labels=["ALPHA-MESH"], done=False)
    assert cli.main(["PMAT-1", "--repo-dir", world["repo"]], once=True) == 0
    wait_for(at_prompt("PMAT-1"), "the worker's hooks to report idle")
    if before == "waiting":
        fire_hook("PMAT-1", "waiting", hook_event_name="Notification", notification_type="idle_prompt",
                  message="Claude is waiting for your input")
    notification = status.read("PMAT-1").get("notification_type")

    fire_hook("PMAT-1", "started", hook_event_name="SessionStart", source="compact")
    s = status.read("PMAT-1")
    assert s["session_start_input"]["source"] == "compact"
    assert (s["state"], s.get("notification_type")) == (before, notification)

    tickets["PMAT-1"] = Ticket(labels=["ALPHA-MESH"], done=False, comment_id="9")
    check(world)
    typed = wait_for(lambda: lines(record / "PMAT-1.typed"), "the comment to be typed")
    assert typed == [loop.REPLY.format(key="PMAT-1", after="the ones from before you started", comment="9")]


def test_start_comment_resume_and_close_end_to_end(world, monkeypatch):
    record, tickets = world["record"], world["tickets"]
    tickets["PMAT-1"] = Ticket(labels=["ALPHA-MESH"], done=False)

    # 1. Start: a tab named after the ticket, running claude with the session ID, name, hooks and worker prompt.
    assert cli.main(["PMAT-1", "--repo-dir", world["repo"]], once=True) == 0
    assert "PMAT-1" in tabs()
    wait_for(at_prompt("PMAT-1"), "the worker's hooks to report idle")
    s = status.read("PMAT-1")
    cwd, argv = wait_for(lambda: lines(record / "PMAT-1.argv"), "the stub to start")[0].split("\t")
    assert cwd == world["repo"] == s["launch_dir"]
    assert f"--session-id {s['session_id']}" in argv and "--name PMAT-1" in argv
    assert f"--settings {loop.HOOKS}" in argv and f"--append-system-prompt-file {loop.WORKER_PROMPT}" in argv
    assert "Work on Jira PMAT-1." in argv
    assert s["session_start_input"]["source"] == "startup"  # written by the real hook

    # 2. A new owner comment on Jira is typed into the tab, once.
    tickets["PMAT-1"] = Ticket(labels=["ALPHA-MESH", "AWAITING-USER-RESPONSE"], done=False, comment_id="9")
    before = turns("PMAT-1")
    check(world)
    check(world)
    typed = wait_for(lambda: lines(record / "PMAT-1.typed"), "the reply to be typed")
    assert typed == [loop.REPLY.format(key="PMAT-1", after="the ones from before you started", comment="9")]
    wait_for(turn_done("PMAT-1", before), "the worker to finish its turn")

    # 3. The tab is killed (as by a reboot): the next check resumes the same session from the same folder.
    tmux("kill-window", "-t", "e2e:PMAT-1")
    check(world)
    assert "PMAT-1" in tabs()
    cwd, argv = wait_for(lambda: lines(record / "PMAT-1.argv")[1:], "the resumed stub to start")[0].split("\t")
    assert cwd == world["repo"] and f"--resume {s['session_id']}" in argv and "interrupted" in argv
    wait_for(lambda: status.read("PMAT-1")["session_start_input"]["source"] == "resume", "the resume hook")
    wait_for(at_prompt("PMAT-1"), "the resumed worker to finish its turn")

    # 4. Done: the tab is warned, then closed (no grace period here), and the ticket is left alone. The worker
    # has removed its worktree, but (as in its sandbox) not git's record of it: closing prunes that record only.
    repo = Path(world["repo"])
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True, text=True).stdout
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init")
    git("worktree", "add", "-q", "--detach", str(repo / "wt-done"))
    git("worktree", "add", "-q", "--detach", str(repo / "wt-live"))
    shutil.rmtree(repo / "wt-done")
    time.sleep(1.1)  # hook times have one-second precision; keep the resume's turn before the warning
    monkeypatch.setattr(loop, "CLOSE_GRACE", loop.timedelta(0))
    tickets["PMAT-1"] = Ticket(labels=["ALPHA-MESH"], done=True)
    before = turns("PMAT-1")
    check(world)
    wait_for(lambda: lines(record / "PMAT-1.typed")[1:] == [loop.CLOSING.format(key="PMAT-1", minutes=0)],
             "the close warning to be typed")
    wait_for(turn_done("PMAT-1", before), "the worker to finish its turn")
    check(world)
    assert "PMAT-1" not in tabs()
    assert status.read("PMAT-1")["finished"] is True and status.active() == []
    assert sorted(p.name for p in (repo / ".git/worktrees").iterdir()) == ["wt-live"]
