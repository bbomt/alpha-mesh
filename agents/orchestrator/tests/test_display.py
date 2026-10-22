"""The orchestrator's screen: what each row tells the owner to do."""

from datetime import datetime, timedelta, timezone

from orchestrator.display import Pr, ago, needs, render
from orchestrator.jira import Ticket

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)


def at(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def need(s=None, t=None, pr=None, alive=True):
    return needs(s or {"state": "idle"}, t, pr, alive, NOW, grace_minutes=5)[0]


def test_owner_actions_are_spelled_out():
    assert need(t=Ticket(labels=["AWAITING-USER-RESPONSE"], done=False)) == "▶ answer on Jira"
    assert need(t=Ticket(labels=["AWAITING-MERGE"], done=False), pr=Pr(24, "OPEN")) == "▶ merge PR #24"
    assert need(s={"state": "waiting", "notification_type": "permission_prompt",
                   "message": "Claude needs your permission to use Bash"}) == \
        "▶ check the tab: Claude needs your permission to use Bash"


def test_idle_and_working_workers_need_nothing():
    assert need(s={"state": "waiting", "notification_type": "idle_prompt"}) == ""
    assert need(s={"state": "working"}, t=Ticket(labels=["ALPHA-MESH"], done=False)) == ""


def test_closing_counts_down_and_a_missing_tab_is_being_resumed():
    assert need(s={"state": "idle", "close_warned_at": at(2)}) == "closing in 3m (type in the tab to keep it)"
    assert need(alive=False) == "resuming the worker"


def test_time_in_state():
    assert (ago(at(4), NOW), ago(at(72), NOW), ago(None, NOW)) == ("4m", "1h12m", "-")


def test_screen_has_a_row_per_worker_and_newest_events_first():
    screen = render(
        ["PMAT-282", "PMAT-283"],
        {"PMAT-282": {"state": "working", "updated": at(1)}, "PMAT-283": {"state": "idle", "updated": at(72)}},
        {"PMAT-282": Ticket(labels=[], done=False), "PMAT-283": Ticket(labels=["AWAITING-MERGE"], done=False)},
        {"PMAT-282": None, "PMAT-283": Pr(24, "OPEN", "https://github.com/bbomt/alpha-mesh/pull/24")},
        {"PMAT-282", "PMAT-283"},
        [(NOW - timedelta(minutes=5), "older event"), (NOW, "newer event")],
        NOW, interval=30, grace_minutes=5, color=False).splitlines()
    assert screen[0].startswith("mesh orchestrate · 2 workers · checked ")
    assert screen[3].split() == ["PMAT-282", "working", "1m", "open", "-"]
    assert screen[4].split() == ["PMAT-283", "idle", "1h12m", "open", "https://github.com/bbomt/alpha-mesh/pull/24",
                                 "open", "▶", "merge", "PR", "#24"]
    assert screen.index("Recent") < [i for i, l in enumerate(screen) if "newer event" in l][0] \
        < [i for i, l in enumerate(screen) if "older event" in l][0]
    assert "\033[" not in "\n".join(screen)


def test_colour_marks_what_needs_the_owner():
    screen = render(["PMAT-1"], {"PMAT-1": {"state": "idle"}},
                    {"PMAT-1": Ticket(labels=["AWAITING-USER-RESPONSE"], done=False)}, {}, {"PMAT-1"}, [],
                    NOW, interval=30, grace_minutes=5, color=True)
    assert "\033[1m\033[33m▶ answer on Jira" in screen


def test_colour_ticket_yellow_done_green_pr_blue():
    screen = render(["PMAT-1"], {"PMAT-1": {"state": "idle"}}, {"PMAT-1": Ticket(labels=[], done=True)},
                    {"PMAT-1": Pr(7, "MERGED", "https://github.com/o/r/pull/7")}, {"PMAT-1"}, [],
                    NOW, interval=30, grace_minutes=5, color=True)
    assert "\033[33mPMAT-1" in screen and "\033[32mDone" in screen
    assert "\033[34mhttps://github.com/o/r/pull/7 merged" in screen
