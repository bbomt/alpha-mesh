"""The orchestrator's screen: what each row tells the owner to do."""

from datetime import datetime, timedelta, timezone

from orchestrator.display import Pr, ago, needs, render, worker
from orchestrator.jira import Ticket

NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)


def at(minutes_ago: int) -> str:
    return (NOW - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def need(s=None, t=None, pr=None, alive=True):
    return needs(s or {"state": "idle"}, t, pr, alive, NOW, grace_minutes=5)[0]


def test_owner_actions_are_spelled_out():
    assert need(t=Ticket(labels=["AWAITING-USER-RESPONSE"], done=False)) == "▶ answer on Jira"
    assert need(t=Ticket(labels=["AWAITING-MERGE"], done=False), pr=Pr(24, "OPEN")) == "▶ approve PR #24"
    # Merged by hand: the worker isn't told by GitHub, so the owner must tell it.
    assert need(t=Ticket(labels=["AWAITING-MERGE"], done=False), pr=Pr(24, "MERGED")) == \
        "▶ tell the worker PR #24 is merged"
    assert need(s={"state": "waiting", "notification_type": "permission_prompt",
                   "message": "Claude needs your permission to use Bash"}) == \
        "▶ check the tab: Claude needs your permission to use Bash"


def test_idle_and_working_workers_need_nothing():
    assert need(s={"state": "waiting", "notification_type": "idle_prompt"}) == ""
    assert need(s={"state": "working"}, t=Ticket(labels=["ALPHA-MESH"], done=False)) == ""


def test_the_60_second_idle_notice_shows_as_idle_not_waiting():
    # Claude's "waiting for your input" notice after a minute at the prompt (e.g. while a background shell
    # runs) is not the worker waiting for the owner; a permission prompt still is.
    assert worker({"state": "waiting", "notification_type": "idle_prompt"}, alive=True)[0] == "idle"
    assert worker({"state": "waiting", "notification_type": "permission_prompt"}, alive=True)[0] == "waiting"


def test_a_worker_at_its_prompt_with_a_background_task_running_is_working():
    assert worker({"state": "idle"}, alive=True, background=True)[0] == "working"
    assert worker({"state": "waiting", "notification_type": "idle_prompt"}, alive=True, background=True)[0] == "working"
    # A real prompt still needs the owner, background task or not.
    assert worker({"state": "waiting", "notification_type": "permission_prompt"}, alive=True,
                  background=True)[0] == "waiting"


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
        NOW, next_in=30, grace_minutes=5, color=False).splitlines()
    cells = [line.strip("│ ").split() for line in screen]
    assert cells.index(["PMAT-282", "working", "1m", "open", "-"]) < \
        cells.index(["PMAT-283", "idle", "1h12m", "open", "https://github.com/bbomt/alpha-mesh/pull/24", "open",
                     "▶", "approve", "PR", "#24"])
    titles = [line for line in screen if line.startswith("┌─ ")]
    assert [t.split()[1] for t in titles] == ["mesh", "Workers", "Recent"]
    assert any("2 workers · checked " in line for line in screen)
    text = "\n".join(screen)
    assert text.index("newer event") < text.index("older event")
    assert "\033[" not in text


def test_boxes_line_up_and_long_events_are_cut():
    screen = render(["PMAT-1"], {"PMAT-1": {"state": "idle"}}, {}, {}, {"PMAT-1"}, [(NOW, "x" * 500)],
                    NOW, next_in=30, grace_minutes=5, color=False).splitlines()
    boxed = [line for line in screen if line[:1] in "┌│└"]
    assert len({len(line) for line in boxed}) == 1  # every box line is the same width
    assert max(len(line) for line in boxed) < 140


def test_width_ignores_colour_codes():
    plain = render(["PMAT-1"], {"PMAT-1": {"state": "idle"}}, {"PMAT-1": Ticket(labels=[], done=True)}, {},
                   {"PMAT-1"}, [], NOW, next_in=30, grace_minutes=5, color=False).splitlines()
    coloured = render(["PMAT-1"], {"PMAT-1": {"state": "idle"}}, {"PMAT-1": Ticket(labels=[], done=True)}, {},
                      {"PMAT-1"}, [], NOW, next_in=30, grace_minutes=5, color=True).splitlines()
    strip = __import__("re").compile(r"\033\[[0-9;]*m")
    assert [strip.sub("", line) for line in coloured] == plain


def test_colour_marks_what_needs_the_owner():
    screen = render(["PMAT-1"], {"PMAT-1": {"state": "idle"}},
                    {"PMAT-1": Ticket(labels=["AWAITING-USER-RESPONSE"], done=False)}, {}, {"PMAT-1"}, [],
                    NOW, next_in=30, grace_minutes=5, color=True)
    assert "\033[1m\033[33m▶ answer on Jira" in screen


def test_colour_ticket_yellow_done_green_pr_blue():
    screen = render(["PMAT-1"], {"PMAT-1": {"state": "idle"}}, {"PMAT-1": Ticket(labels=[], done=True)},
                    {"PMAT-1": Pr(7, "MERGED", "https://github.com/o/r/pull/7")}, {"PMAT-1"}, [],
                    NOW, next_in=30, grace_minutes=5, color=True)
    assert "\033[33mPMAT-1" in screen and "\033[32mDone" in screen
    assert "\033[34mhttps://github.com/o/r/pull/7 merged" in screen


def test_recent_events_are_dimmed():
    screen = render([], {}, {}, {}, set(), [(NOW, "PMAT-1: started a worker")], NOW, next_in=30, grace_minutes=5,
                    color=True)
    assert "\033[2m" + f"{NOW.astimezone():%H:%M:%S}  PMAT-1: started a worker" in screen


def test_failed_events_are_red():
    screen = render([], {}, {}, {}, set(), [(NOW, "Jira read failed, trying again next tick: boom")], NOW,
                    next_in=30, grace_minutes=5, color=True)
    assert "\033[31m" + f"{NOW.astimezone():%H:%M:%S}  Jira read failed" in screen


def test_a_rule_runs_under_the_column_headings():
    screen = render(["PMAT-1"], {"PMAT-1": {"state": "idle"}}, {}, {}, {"PMAT-1"}, [], NOW, next_in=30,
                    grace_minutes=5, color=False).splitlines()
    heading = next(i for i, line in enumerate(screen) if "TICKET" in line)
    assert set(screen[heading + 1].strip("│ ")) == {"─"}
    assert len(screen[heading + 1]) == len(screen[heading])
    assert "PMAT-1" in screen[heading + 2]


def test_the_header_line_is_dimmed():
    screen = render([], {}, {}, {}, set(), [], NOW, next_in=8, grace_minutes=5, color=True)
    assert f"\033[2m0 workers · checked {NOW.astimezone():%H:%M:%S} · next check in 8s" in screen
