"""The mesh orchestrator's decisions, against fake tmux, gh and Jira and a throwaway status folder."""

from datetime import datetime, timedelta, timezone

import pytest

from orchestrator import loop, status
from orchestrator.jira import Ticket


class Fake:
    def __init__(self):
        self.tabs, self.opened, self.sent, self.killed, self.pruned = set(), [], [], [], []
        self.clock = self.start = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
        self.lost = False  # True: typing reaches no prompt (the tab's input in history mode, PMAT-337)

    def send(self, name, text):
        self.sent.append((name, text))
        if not self.lost:  # the worker's UserPromptSubmit hook records the prompt
            s = status.read(name)
            status.update(name, state="working", history=s.get("history", []) + [
                {"event": "UserPromptSubmit", "at": self.clock.isoformat(timespec="seconds")}])

    def sleep(self, seconds):
        self.clock += timedelta(seconds=seconds)

    def at_prompt(self, key):
        """The worker's turn ended (its Stop hook)."""
        status.update(key, state="idle")

    def ops(self):
        return loop.Ops(
            windows=lambda: set(self.tabs),
            new_window=lambda name, cwd, cmd: (self.tabs.add(name), self.opened.append((name, cwd, cmd))),
            send=self.send,
            kill=lambda name: (self.tabs.discard(name), self.killed.append(name)),
            now=lambda: self.clock,
            prune=lambda folder: self.pruned.append(folder),
            sleep=self.sleep,
        )

    def prompt_at(self, key, minutes):
        """The worker received a prompt (UserPromptSubmit hook) this many minutes after the clock's start."""
        at = (self.start + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        s = status.read(key)
        status.update(key, history=s.get("history", []) + [{"event": "UserPromptSubmit", "at": at}])


@pytest.fixture(autouse=True)
def status_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(status, "DIR", tmp_path)


@pytest.fixture
def running():
    """PMAT-1 started, its worker at its prompt."""
    fake = Fake()
    loop.start("PMAT-1", "/repo", fake.ops())
    status.update("PMAT-1", state="idle", relayed_comment=None)  # first look done: no comments yet
    return fake


def test_start_saves_the_session_before_opening_the_tab():
    fake = Fake()
    loop.start("PMAT-1", "/repo", fake.ops())
    s = status.read("PMAT-1")
    name, cwd, cmd = fake.opened[0]
    assert (name, cwd, s["launch_dir"]) == ("PMAT-1", "/repo", "/repo")
    assert f"--session-id {s['session_id']}" in cmd and "--name PMAT-1" in cmd
    assert cmd.startswith("ORCH_TICKET=PMAT-1 claude ") and "--settings " in cmd
    assert f"--append-system-prompt-file {loop.WORKER_PROMPT}" in cmd and loop.WORKER_PROMPT.exists()
    assert "'Work on Jira PMAT-1.'" in cmd
    assert status.active() == ["PMAT-1"]


def test_missing_tab_is_resumed_in_the_launch_folder(running):
    running.tabs.clear()  # a reboot or a closed tab
    out = loop.tick(["PMAT-1"], {}, running.ops())
    name, cwd, cmd = running.opened[-1]
    assert (name, cwd) == ("PMAT-1", "/repo")
    assert f"--resume {status.read('PMAT-1')['session_id']}" in cmd and "interrupted" in cmd
    assert f"--append-system-prompt-file {loop.WORKER_PROMPT}" in cmd
    assert out == ["PMAT-1: tab missing, resumed the worker"]


def test_owner_reply_is_passed_on_once(running):
    t = {"PMAT-1": Ticket(labels=["AWAITING-USER-RESPONSE"], done=False, comment_id="9")}
    loop.tick(["PMAT-1"], t, running.ops())
    loop.tick(["PMAT-1"], t, running.ops())
    assert running.sent == [("PMAT-1", loop.REPLY.format(key="PMAT-1", after="the ones from before you started", comment="9"))]


def test_the_worker_is_told_which_comments_are_new(running):
    # PMAT-221: 13226 was passed on; while the worker worked on it the owner posted 13227 and the worker then
    # posted 13228. "Since my last post" (13228) would miss 13227, so the message names the range.
    status.update("PMAT-1", relayed_comment="13226")
    loop.tick(["PMAT-1"], {"PMAT-1": Ticket(labels=[], done=False, comment_id="13227")}, running.ops())
    assert running.sent == [("PMAT-1", "The owner commented on Jira PMAT-1: comments after 13226, newest 13227. "
                                       "Read them all and act on them.")]


def test_owner_comment_is_passed_on_whatever_the_labels(running):
    t = {"PMAT-1": Ticket(labels=["AWAITING-MERGE"], done=False, comment_id="12")}  # e.g. merge rejected
    loop.tick(["PMAT-1"], t, running.ops())
    assert running.sent == [("PMAT-1", loop.REPLY.format(key="PMAT-1", after="the ones from before you started", comment="12"))]


def test_comments_from_before_the_worker_started_are_not_passed_on():
    fake = Fake()
    loop.start("PMAT-1", "/repo", fake.ops())
    t = {"PMAT-1": Ticket(labels=[], done=False, comment_id="5")}
    loop.tick(["PMAT-1"], t, fake.ops())
    status.update("PMAT-1", state="idle")
    loop.tick(["PMAT-1"], t, fake.ops())
    assert fake.sent == []


def test_worker_question_alone_is_not_a_reply(running):
    # Only the worker's question since: the owner's newest comment is still the one already seen at start.
    t = {"PMAT-1": Ticket(labels=["AWAITING-USER-RESPONSE"], done=False, comment_id=None)}
    loop.tick(["PMAT-1"], t, running.ops())
    assert running.sent == []


def test_nothing_is_typed_while_the_worker_is_busy(running):
    status.update("PMAT-1", state="working")
    t = {"PMAT-1": Ticket(labels=["AWAITING-USER-RESPONSE"], done=False, comment_id="9")}
    loop.tick(["PMAT-1"], t, running.ops())
    assert running.sent == []


DONE = {"PMAT-1": Ticket(labels=[], done=True)}


def test_done_ticket_gets_a_warning_first_not_a_close(running):
    loop.tick(["PMAT-1"], DONE, running.ops())
    assert running.killed == []
    assert running.sent == [("PMAT-1", loop.CLOSING.format(key="PMAT-1", minutes=5))]


def test_done_tab_closes_after_the_grace_period_if_nobody_typed(running):
    loop.tick(["PMAT-1"], DONE, running.ops())  # the warning, received by the worker
    running.at_prompt("PMAT-1")
    running.clock += timedelta(minutes=4)
    loop.tick(["PMAT-1"], DONE, running.ops())
    assert running.killed == [] and running.pruned == []
    running.clock += timedelta(minutes=1)
    loop.tick(["PMAT-1"], DONE, running.ops())
    assert running.killed == ["PMAT-1"] and status.active() == []
    # The worker removed its worktree inside its sandbox, which can't delete git's record of it; prune it.
    assert running.pruned == [status.read("PMAT-1")["launch_dir"]]


def test_owner_typing_after_the_warning_keeps_the_tab(running):
    loop.tick(["PMAT-1"], DONE, running.ops())  # the warning, received by the worker
    running.at_prompt("PMAT-1")
    running.prompt_at("PMAT-1", 2)  # the owner
    running.clock += timedelta(minutes=10)
    out = loop.tick(["PMAT-1"], DONE, running.ops())
    assert running.killed == [] and status.active() == []  # left alone from now on
    assert out == ["PMAT-1: Done, the owner kept the tab; it is theirs now"]


def test_a_warned_tab_lost_in_a_reboot_is_warned_again_not_taken_as_kept(running):
    loop.tick(["PMAT-1"], DONE, running.ops())  # our warning, received
    running.tabs.clear()  # reboot
    running.clock += timedelta(minutes=6)
    loop.tick(["PMAT-1"], DONE, running.ops())  # resumed
    running.prompt_at("PMAT-1", 6)  # the resume message
    running.at_prompt("PMAT-1")
    loop.tick(["PMAT-1"], DONE, running.ops())
    assert running.killed == [] and status.active() == ["PMAT-1"]
    assert running.sent[-1] == ("PMAT-1", loop.CLOSING.format(key="PMAT-1", minutes=5))


def test_done_with_an_open_question_gets_no_warning(running):
    loop.tick(["PMAT-1"], {"PMAT-1": Ticket(labels=["AWAITING-USER-RESPONSE"], done=True)}, running.ops())
    assert running.sent == [] and running.killed == []


def test_done_while_the_worker_is_busy_gets_no_warning(running):
    status.update("PMAT-1", state="working")
    loop.tick(["PMAT-1"], DONE, running.ops())
    assert running.sent == []


def test_one_tickets_failure_does_not_stop_the_others(running):
    loop.start("PMAT-2", "/repo", running.ops())
    status.update("PMAT-2", state="idle", relayed_comment=None)
    ops = running.ops()

    def send(name, text):
        if name == "PMAT-1":
            raise RuntimeError("can't find window: PMAT-1")
        running.send(name, text)

    ops.send = send
    t = {"PMAT-1": Ticket(labels=[], done=False, comment_id="6"),
         "PMAT-2": Ticket(labels=[], done=False, comment_id="7")}
    out = loop.tick(["PMAT-1", "PMAT-2"], t, ops)
    assert out[0].startswith("PMAT-1: failed this tick") and "can't find window" in out[0]
    assert running.sent == [("PMAT-2", loop.REPLY.format(key="PMAT-2", after="the ones from before you started", comment="7"))]


def test_hook_fields_and_orchestrator_fields_survive_each_other():
    status.update("PMAT-1", launch_dir="/repo")
    status.update("PMAT-1", state="idle")
    assert status.read("PMAT-1") == {"launch_dir": "/repo", "state": "idle"}


# PMAT-337: a comment counts as passed on only once the worker received it as a prompt (its UserPromptSubmit
# hook). On 2026-11-13 PMAT-312's tab was in Claude Code's history mode, so the typed relay was never submitted.
COMMENT = {"PMAT-1": Ticket(labels=["AWAITING-USER-RESPONSE"], done=False, comment_id="13765")}
RELAY = loop.REPLY.format(key="PMAT-1", after="the ones from before you started", comment="13765")


def test_a_lost_relay_is_not_counted_as_passed_on_and_is_typed_again(running):
    running.lost = True
    out = loop.tick(["PMAT-1"], COMMENT, running.ops())
    assert status.read("PMAT-1")["relayed_comment"] is None
    assert out == ["PMAT-1: comment 13765 not received (the tab's input may be in use), trying again next check"]
    loop.tick(["PMAT-1"], COMMENT, running.ops())
    assert running.sent == [("PMAT-1", RELAY), ("PMAT-1", RELAY)]


def test_a_delivered_relay_is_passed_on_as_before(running):
    out = loop.tick(["PMAT-1"], COMMENT, running.ops())
    s = status.read("PMAT-1")
    assert s["relayed_comment"] == "13765" and s["state"] == "working" and not s.get("undelivered")
    assert out == ["PMAT-1: passed on the owner's comment 13765"]


def test_a_relay_that_arrives_on_the_second_try_is_passed_on(running):
    running.lost = True
    loop.tick(["PMAT-1"], COMMENT, running.ops())
    running.lost = False  # the owner left history mode
    out = loop.tick(["PMAT-1"], COMMENT, running.ops())
    assert out == ["PMAT-1: passed on the owner's comment 13765"]
    assert status.read("PMAT-1")["relayed_comment"] == "13765" and not status.read("PMAT-1")["undelivered"]


def test_after_the_retry_cap_it_stops_typing_and_the_screen_says_check_the_tab(running):
    from orchestrator import display
    running.lost = True
    outs = [loop.tick(["PMAT-1"], COMMENT, running.ops()) for _ in range(loop.MAX_ATTEMPTS + 2)]
    assert running.sent == [("PMAT-1", RELAY)] * loop.MAX_ATTEMPTS
    assert outs[loop.MAX_ATTEMPTS - 1] == ["PMAT-1: comment 13765 not delivered: check the tab"]
    assert outs[loop.MAX_ATTEMPTS:] == [[], []]  # no event spam while it waits
    s = status.read("PMAT-1")
    assert s["relayed_comment"] is None
    assert display.needs(s, COMMENT["PMAT-1"], None, True, running.clock, 5)[0] == \
        "▶ comment 13765 not delivered: check the tab"
    # The owner fixes the tab and types there; at the prompt again, the relay is typed once more and arrives.
    running.lost = False
    running.clock += timedelta(minutes=1)
    running.prompt_at("PMAT-1", 10)
    out = loop.tick(["PMAT-1"], COMMENT, running.ops())
    assert out == ["PMAT-1: passed on the owner's comment 13765"] and len(running.sent) == loop.MAX_ATTEMPTS + 1
    assert display.needs(status.read("PMAT-1"), COMMENT["PMAT-1"], None, True, running.clock, 5)[0] == "▶ answer on Jira"


def test_a_lost_closing_warning_does_not_start_the_grace_period(running):
    running.lost = True
    out = loop.tick(["PMAT-1"], DONE, running.ops())
    assert out == ["PMAT-1: the closing warning not received (the tab's input may be in use), trying again next check"]
    assert not status.read("PMAT-1").get("close_warned_at")
    running.clock += timedelta(minutes=10)
    loop.tick(["PMAT-1"], DONE, running.ops())
    assert running.killed == [] and len(running.sent) == 2  # typed again, never closed
