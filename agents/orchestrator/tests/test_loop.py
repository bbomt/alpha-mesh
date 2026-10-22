"""The team lead's decisions, against fake tmux, gh and Jira and a throwaway status folder."""

import pytest

from orchestrator import loop, status
from orchestrator.jira import Ticket


class Fake:
    def __init__(self, merged=False):
        self.tabs, self.opened, self.sent, self.killed, self.is_merged = set(), [], [], [], merged

    def ops(self):
        return loop.Ops(
            windows=lambda: set(self.tabs),
            new_window=lambda name, cwd, cmd: (self.tabs.add(name), self.opened.append((name, cwd, cmd))),
            send=lambda name, text: self.sent.append((name, text)),
            kill=lambda name: (self.tabs.discard(name), self.killed.append(name)),
            merged=lambda key, repo: self.is_merged,
        )


@pytest.fixture(autouse=True)
def status_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "DIR", tmp_path)


@pytest.fixture
def running():
    """PMAT-1 started, its worker at its prompt."""
    fake = Fake()
    loop.start("PMAT-1", "/repo", fake.ops())
    status.update("PMAT-1", state="idle")
    return fake


def test_start_saves_the_session_before_opening_the_tab():
    fake = Fake()
    loop.start("PMAT-1", "/repo", fake.ops())
    s = status.read("PMAT-1")
    name, cwd, cmd = fake.opened[0]
    assert (name, cwd, s["launch_dir"]) == ("PMAT-1", "/repo", "/repo")
    assert f"--session-id {s['session_id']}" in cmd and "--name PMAT-1" in cmd
    assert cmd.startswith("ORCH_TICKET=PMAT-1 claude ") and "--settings " in cmd
    assert "'Work on Jira PMAT-1.'" in cmd
    assert status.active() == ["PMAT-1"]


def test_missing_tab_is_resumed_in_the_launch_folder(running):
    running.tabs.clear()  # a reboot or a closed tab
    out = loop.tick(["PMAT-1"], {}, running.ops())
    name, cwd, cmd = running.opened[-1]
    assert (name, cwd) == ("PMAT-1", "/repo")
    assert f"--resume {status.read('PMAT-1')['session_id']}" in cmd and "interrupted" in cmd
    assert out == ["PMAT-1: tab missing, resumed the worker"]


def test_owner_reply_is_passed_on_once(running):
    t = {"PMAT-1": Ticket(labels=["AWAITING-USER-RESPONSE"], done=False, comment_id="9")}
    loop.tick(["PMAT-1"], t, running.ops())
    loop.tick(["PMAT-1"], t, running.ops())
    assert running.sent == [("PMAT-1", loop.REPLY.format(key="PMAT-1", comment="9"))]


def test_worker_question_alone_is_not_a_reply(running):
    t = {"PMAT-1": Ticket(labels=["AWAITING-USER-RESPONSE"], done=False, comment_id="8", comment_from_worker=True)}
    loop.tick(["PMAT-1"], t, running.ops())
    assert running.sent == []


def test_nothing_is_typed_while_the_worker_is_busy(running):
    status.update("PMAT-1", state="working")
    t = {"PMAT-1": Ticket(labels=["AWAITING-USER-RESPONSE"], done=False, comment_id="9")}
    loop.tick(["PMAT-1"], t, running.ops())
    assert running.sent == []


def test_merge_is_passed_on_only_once_the_pr_is_merged(running):
    t = {"PMAT-1": Ticket(labels=["AWAITING-MERGE"], done=False)}
    loop.tick(["PMAT-1"], t, running.ops())
    assert running.sent == []
    running.is_merged = True
    loop.tick(["PMAT-1"], t, running.ops())
    loop.tick(["PMAT-1"], t, running.ops())
    assert running.sent == [("PMAT-1", loop.MERGED.format(key="PMAT-1"))]


def test_done_ticket_tab_is_closed_and_left_alone(running):
    status.update("PMAT-1", merge_relayed=True)
    loop.tick(["PMAT-1"], {"PMAT-1": Ticket(labels=[], done=True)}, running.ops())
    assert running.killed == ["PMAT-1"]
    assert status.active() == []


def test_done_without_a_merge_from_us_keeps_the_tab(running):
    loop.tick(["PMAT-1"], {"PMAT-1": Ticket(labels=[], done=True)}, running.ops())
    assert running.killed == []


def test_hook_fields_and_orchestrator_fields_survive_each_other():
    status.update("PMAT-1", launch_dir="/repo")
    status.update("PMAT-1", state="idle")
    assert status.read("PMAT-1") == {"launch_dir": "/repo", "state": "idle"}


def test_table_shows_one_row_per_ticket(running):
    out = loop.table(["PMAT-1"], {"PMAT-1": Ticket(labels=["ALPHA-MESH"], done=False)}, running.ops())
    assert out.splitlines()[1].split() == ["PMAT-1", "open", "idle", "open", "ALPHA-MESH"]
