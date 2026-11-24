"""PMAT-355 acceptance tests, at the `mesh orchestrate` boundary (cli.main) with fake tmux, Jira and clock: the cap
changed while running (pausing workers above it), pause and resume, and --priority."""

import fcntl
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from orchestrator import cli, jira, loop, status

GRACE = loop.CLOSE_GRACE


class World:
    """Fake tmux (tabs; typing records the worker's UserPromptSubmit hook), Jira and clock."""

    def __init__(self, tmp_path):
        self.tabs, self.opened, self.sent, self.killed = set(), [], [], []
        self.tickets: dict[str, jira.Ticket] = {}
        self.clock = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
        self.repo = tmp_path / "repo"
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def new_window(self, name, cwd, cmd):
        self.tabs.add(name)
        self.opened.append((name, cwd, cmd))

    def send(self, name, text):
        self.sent.append((name, text))
        self.typed(name)

    def typed(self, name):
        """A prompt reached the worker: the orchestrator's, or the owner typing in the tab."""
        s = status.read(name)
        status.update(name, state="working", history=s.get("history", []) + [
            {"event": "UserPromptSubmit", "at": self.clock.isoformat(timespec="seconds")}])

    def ops(self):
        return loop.Ops(windows=lambda: set(self.tabs), new_window=self.new_window, send=self.send,
                        kill=lambda name: (self.tabs.discard(name), self.killed.append(name)),
                        now=lambda: self.clock, sleep=lambda s: None)

    def idle(self):
        """Every running worker's turn ended (its Stop hook): at its prompt."""
        for key in status.active():
            status.update(key, state="idle")

    def later(self, minutes):
        self.clock += timedelta(minutes=minutes)

    def warned(self):
        return [name for name, text in self.sent if text.startswith("Mesh orchestrator: I am pausing")]


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "DIR", tmp_path / "status")
    w = World(tmp_path)

    async def snapshot(keys):
        return {k: w.tickets.get(k, jira.Ticket(labels=[], done=False)) for k in keys}

    monkeypatch.setattr(cli.jira, "snapshot", snapshot)
    monkeypatch.setattr(cli.jira, "logged_in", lambda: True)
    monkeypatch.setattr(cli, "prs", lambda keys, repo_dir: {})
    monkeypatch.setattr(cli, "OPS", w.ops())
    return w


def check(*argv) -> int:
    """One check of the orchestrator (a restart with these arguments, then one loop)."""
    return cli.main(list(argv), once=True)


def second(*argv) -> int:
    """`mesh orchestrate ...` from another shell while the orchestrator runs (it holds the lock)."""
    status.DIR.mkdir(parents=True, exist_ok=True)
    with open(status.DIR / "orchestrator.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        return cli.main(list(argv))


def start(world, *keys):
    """Start each ticket in its own check, a minute apart, so they have a start order; then all at their prompt."""
    for key in keys:
        assert check(key, "--repo-dir", str(world.repo)) == 0
        world.later(1)
    world.idle()


def queue():
    return [(k, "paused" if status.read(k).get("paused") else "") for k, _ in status.queued()]


def events():
    return (status.DIR / "events.log").read_text()


def test_1_cap_set_while_running_pauses_the_newest_workers(world, capsys):
    start(world, "PMAT-1", "PMAT-2", "PMAT-3")
    assert check("PMAT-4", "--repo-dir", str(world.repo)) == 0  # cap 3: queued
    capsys.readouterr()

    assert second("--max-workers", "1") == 0
    out = capsys.readouterr().out
    assert "cap set to 1" in out and "running orchestrator uses it from its next check" in out
    assert world.tabs == {"PMAT-1", "PMAT-2", "PMAT-3"}  # nothing happens until the running copy's next check

    assert check() == 0
    assert world.warned() == ["PMAT-3", "PMAT-2"]  # newest first
    assert "PMAT-3: pausing (cap 1), closing in 5 min" in events()
    world.later(GRACE.seconds // 60)
    assert check() == 0
    assert world.tabs == {"PMAT-1"}
    # Pausing never changes a ticket's place: the queue is in the order the tickets were added (comment 13928).
    assert queue() == [("PMAT-2", "paused"), ("PMAT-3", "paused"), ("PMAT-4", "")]
    assert check() == 0 and world.tabs == {"PMAT-1"}  # at the cap: nothing more starts
    out = capsys.readouterr().out
    assert "Workers 1/1" in out and "Queue (max workers 1)" in out and "1. PMAT-2  paused" in out


def test_2_the_owner_typing_during_the_grace_cancels_that_pause(world):
    start(world, "PMAT-1", "PMAT-2", "PMAT-3")
    assert second("--max-workers", "2") == 0
    assert check() == 0 and world.warned() == ["PMAT-3"]
    world.later(1)
    world.typed("PMAT-3")  # the owner, in PMAT-3's tab
    assert check() == 0
    assert "PMAT-3: pause cancelled (owner typed)" in events()
    world.idle()
    assert check() == 0  # the next check picks the next most recently started worker
    assert world.warned() == ["PMAT-3", "PMAT-2"]
    world.later(GRACE.seconds // 60)
    assert check() == 0
    assert world.tabs == {"PMAT-1", "PMAT-3"} and queue() == [("PMAT-2", "paused")]


def test_3_the_warning_asks_for_a_jira_comment_and_its_absence_is_logged(world):
    start(world, "PMAT-1", "PMAT-2")
    assert second("--max-workers", "1") == 0
    assert check() == 0
    warning = dict(world.sent)["PMAT-2"]
    assert "MESSAGE-FROM-WORKER comment on Jira PMAT-2" in warning and "stop any running processes" in warning
    world.later(GRACE.seconds // 60)
    assert check() == 0  # the fake Jira has no worker comment
    assert "PMAT-2" not in world.tabs
    assert "PMAT-2: paused without a Jira comment" in events()


def test_4_a_paused_worker_resumes_its_same_session_in_its_own_folder(world):
    start(world, "PMAT-1", "PMAT-3")
    session = status.read("PMAT-3")["session_id"]
    assert second("--max-workers", "1") == 0
    assert check() == 0
    world.later(2)  # the worker posts its status during the grace
    world.tickets["PMAT-3"] = jira.Ticket(labels=[], done=False,
                                          worker_comment=("13999", (world.clock).isoformat()))
    world.later(GRACE.seconds // 60)
    assert check() == 0
    assert "PMAT-3: paused" in events() and "without a Jira comment" not in events()
    assert queue() == [("PMAT-3", "paused")]

    # PMAT-1 is Done; its close warning runs out and its tab closes; the next check resumes PMAT-3.
    world.tickets["PMAT-1"] = jira.Ticket(labels=[], done=True)
    status.update("PMAT-1", state="idle", relayed_comment=None)
    assert check() == 0  # first look at PMAT-1's comments
    assert check() == 0  # Done: warned
    world.idle()
    world.later(GRACE.seconds // 60)
    assert check() == 0 and "PMAT-1" not in world.tabs
    assert check() == 0
    name, cwd, cmd = world.opened[-1]
    assert name == "PMAT-3" and cwd == str(world.repo)
    assert f"--resume {session}" in cmd and "--session-id" not in cmd
    assert "13999" in cmd and "paused" in cmd  # the resume line names its last Jira comment
    assert "PMAT-3: resumed after pause" in events()
    assert status.active() == ["PMAT-3"] and queue() == []


def test_5_priority_goes_to_the_front_and_pauses_the_newest_worker_for_room(world, capsys):
    start(world, "PMAT-1", "PMAT-2")
    assert check("PMAT-3", "--repo-dir", str(world.repo), "--max-workers", "2") == 0  # queued
    assert second("--max-workers", "1") == 0
    assert check() == 0
    world.later(GRACE.seconds // 60)
    assert check() == 0
    assert world.tabs == {"PMAT-1"} and queue() == [("PMAT-2", "paused"), ("PMAT-3", "")]  # cap 1: [A], [B(p), C]

    assert second("--priority", "PMAT-3") == 0
    assert "PMAT-3 is the priority ticket" in capsys.readouterr().out
    assert queue() == [("PMAT-3", ""), ("PMAT-2", "paused")] and status.priority() == "PMAT-3"
    assert "PMAT-3: priority, moved to the front of the queue" in events()
    world.idle()
    assert check() == 0
    assert world.warned()[-1] == "PMAT-1" and "PMAT-1: pausing (room for priority PMAT-3)" in events()
    world.later(GRACE.seconds // 60)
    assert check() == 0
    assert check() == 0
    assert world.tabs == {"PMAT-3"} and queue() == [("PMAT-1", "paused"), ("PMAT-2", "paused")]

    # --priority on the running priority ticket changes nothing.
    assert second("--priority", "PMAT-3") == 0
    assert "nothing changed" in capsys.readouterr().out
    assert queue() == [("PMAT-1", "paused"), ("PMAT-2", "paused")]

    # A new ticket as priority needs --repo-dir; it takes the mark from PMAT-3, now an ordinary worker, which is
    # paused to make room for it (owner, PMAT-355 comment 13925).
    with pytest.raises(SystemExit):
        second("--priority", "PMAT-4")
    assert second("--priority", "PMAT-4", "--repo-dir", str(world.repo)) == 0
    assert status.priority() == "PMAT-4" and queue()[0] == ("PMAT-4", "")
    world.idle()
    assert check() == 0
    assert "PMAT-3: pausing (room for priority PMAT-4)" in events()
    assert "Workers 1/1 · priority PMAT-4" in capsys.readouterr().out


def test_6_the_owners_scenario_one_ticket_alone(world):
    start(world, "PMAT-1", "PMAT-2", "PMAT-3")
    assert second("--max-workers", "1") == 0
    assert second("--priority", "PMAT-9", "--repo-dir", str(world.repo)) == 0
    assert check() == 0
    assert world.warned() == ["PMAT-3", "PMAT-2", "PMAT-1"]
    world.later(GRACE.seconds // 60)
    assert check() == 0
    assert check() == 0
    assert world.tabs == {"PMAT-9"}
    assert queue() == [("PMAT-1", "paused"), ("PMAT-2", "paused"), ("PMAT-3", "paused")]
    log = events()
    assert all(f"PMAT-{n}: pausing" in log and f"PMAT-{n}: paused" in log for n in (1, 2, 3))


def test_7_the_priority_ticket_is_never_paused_until_it_loses_the_mark(world):
    start(world, "PMAT-1", "PMAT-2")
    assert second("--priority", "PMAT-2") == 0  # running: marked, nothing else changes
    assert queue() == []
    assert second("--max-workers", "1") == 0
    assert check() == 0
    assert world.warned() == ["PMAT-1"]  # not PMAT-2, though it started last
    world.later(GRACE.seconds // 60)
    assert check() == 0 and world.tabs == {"PMAT-2"}

    # A newer priority takes the mark: PMAT-2 is an ordinary worker now (owner, PMAT-355 comment 13925), so it is
    # paused to make room, and PMAT-5 runs alone.
    assert second("--priority", "PMAT-5", "--repo-dir", str(world.repo)) == 0
    world.idle()
    assert check() == 0
    assert world.warned() == ["PMAT-1", "PMAT-2"]
    world.later(GRACE.seconds // 60)
    assert check() == 0
    assert check() == 0
    assert world.tabs == {"PMAT-5"} and queue() == [("PMAT-1", "paused"), ("PMAT-2", "paused")]


def test_8_a_restart_keeps_the_cap_queue_priority_and_paused_marks(world, capsys):
    start(world, "PMAT-1", "PMAT-2", "PMAT-3")
    assert check("PMAT-4", "--repo-dir", str(world.repo)) == 0
    assert second("--priority", "PMAT-1") == 0
    assert second("--max-workers", "1") == 0
    assert check() == 0
    world.later(GRACE.seconds // 60)
    assert check() == 0
    opened = len(world.opened)
    capsys.readouterr()

    # Ctrl-C, then `mesh orchestrate` with no flags.
    assert check() == 0
    out = capsys.readouterr().out
    assert status.cap() == 1 and status.priority() == "PMAT-1"
    assert queue() == [("PMAT-2", "paused"), ("PMAT-3", "paused"), ("PMAT-4", "")]
    assert len(world.opened) == opened and world.tabs == {"PMAT-1"}  # no paused ticket started afresh
    assert "Workers 1/1 · priority PMAT-1" in out and "2. PMAT-3  paused" in out


def test_a_restart_with_a_lower_cap_pauses_workers_already_running(world):
    # Cause 2 of the bug: a lower cap only stopped new starts, so 3 tabs stayed open.
    start(world, "PMAT-1", "PMAT-2", "PMAT-3")
    assert check("--max-workers", "1") == 0
    world.later(GRACE.seconds // 60)
    assert check() == 0
    assert world.tabs == {"PMAT-1"} and status.cap() == 1


def test_a_worker_chosen_for_a_pause_whose_tab_is_gone_is_paused_not_resumed(world):
    start(world, "PMAT-1", "PMAT-2")
    for key in ("PMAT-1", "PMAT-2"):  # both busy: PMAT-2 (latest) chosen, not yet warned
        status.update(key, state="working")
    assert second("--max-workers", "1") == 0
    assert check() == 0 and world.warned() == []
    assert "pause pending (busy)" in cli.display.needs(status.read("PMAT-2"), None, None, True, world.clock, 5)[0]
    world.tabs.discard("PMAT-2")  # closed, or a reboot

    def kill(name):  # tmux refuses to kill a missing tab
        raise RuntimeError(f"can't find window: {name}")

    cli.OPS.kill = kill
    assert check() == 0
    assert queue() == [("PMAT-2", "paused")] and "PMAT-2: paused" in events()
    assert [name for name, *_ in world.opened].count("PMAT-2") == 1  # never reopened just to be paused



def test_the_owners_order_example_priority_first_then_the_order_tickets_were_added(world):
    # Owner, comment 13928: tickets [A, B, C], then --priority P --max-workers 2 gives [P, A, B(paused), C(paused)].
    start(world, "PMAT-1", "PMAT-2", "PMAT-3")
    assert second("--priority", "PMAT-9", "--repo-dir", str(world.repo), "--max-workers", "2") == 0
    assert check() == 0
    assert world.warned() == ["PMAT-3", "PMAT-2"]
    world.later(GRACE.seconds // 60)
    assert check() == 0
    assert check() == 0
    assert world.tabs == {"PMAT-9", "PMAT-1"}
    assert status.order() == ["PMAT-1", "PMAT-2", "PMAT-3", "PMAT-9"]
    assert queue() == [("PMAT-2", "paused"), ("PMAT-3", "paused")]


def test_a_worker_at_its_prompt_is_paused_before_a_busy_one_later_in_the_order(world):
    # Owner, PMAT-355 (option b; comment 13928's second example): [P, A(paused), B, C(paused)] when B is busy.
    start(world, "PMAT-1", "PMAT-2", "PMAT-3")
    status.update("PMAT-2", state="working")
    assert second("--priority", "PMAT-9", "--repo-dir", str(world.repo), "--max-workers", "2") == 0
    assert check() == 0
    assert world.warned() == ["PMAT-3", "PMAT-1"]  # PMAT-2 is busy: PMAT-1, at its prompt, goes instead
    world.later(GRACE.seconds // 60)
    assert check() == 0
    assert check() == 0
    assert world.tabs == {"PMAT-9", "PMAT-2"} and queue() == [("PMAT-1", "paused"), ("PMAT-3", "paused")]


def test_when_every_candidate_is_busy_the_pause_waits_and_moves_to_the_first_at_its_prompt(world):
    start(world, "PMAT-1", "PMAT-2")
    for key in ("PMAT-1", "PMAT-2"):
        status.update(key, state="working")
    assert second("--max-workers", "1") == 0
    assert check() == 0
    assert world.warned() == [] and status.read("PMAT-2")["pausing"]["reason"] == "cap 1"  # latest, waiting
    status.update("PMAT-1", state="idle")  # PMAT-1 reaches its prompt first
    assert check() == 0
    assert world.warned() == ["PMAT-1"] and not status.read("PMAT-2").get("pausing")
