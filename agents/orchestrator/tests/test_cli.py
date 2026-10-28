"""The loop around the ticks, and the real tmux wrapper's parsing."""

import json
import os
import re
import subprocess

import pytest

from orchestrator import cli, jira, loop, status, tmux
from orchestrator.display import Pr


def test_a_failing_tick_does_not_stop_the_orchestrator(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(status, "DIR", tmp_path)

    def broken(*args):
        raise RuntimeError("can't find window: PMAT-1")

    monkeypatch.setattr(loop, "tick", broken)
    assert cli.main([], once=True) == 0
    assert "Tick failed, trying again next tick" in capsys.readouterr().out  # shown under Recent


def test_window_names_with_spaces_are_kept_whole(monkeypatch):
    monkeypatch.setattr(tmux, "_tmux", lambda *args: "PMAT-1 notes\nshell\n")
    assert tmux.windows() == {"PMAT-1 notes", "shell"}


def test_recent_events_survive_a_restart(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(status, "DIR", tmp_path)
    (tmp_path / "events.log").write_text("2026-10-22T15:15:20+00:00  PMAT-282: passed on the owner's comment 13103\n")
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=set, new_window=None, send=None, kill=None))
    assert cli.main([], once=True) == 0
    assert "PMAT-282: passed on the owner's comment 13103" in capsys.readouterr().out


def test_prs_match_the_bracketed_ticket_key_and_take_the_newest(monkeypatch):
    listed = [{"number": 9, "state": "OPEN", "url": "u9", "title": "feature[PMAT-2820]: other"},
              {"number": 10, "state": "OPEN", "url": "u10", "title": "feature[PMAT-283]: test app (for PMAT-282)"},
              {"number": 8, "state": "OPEN", "url": "u8", "title": "fix[PMAT-282]: second try"},
              {"number": 7, "state": "CLOSED", "url": "u7", "title": "feature[PMAT-282]: first try"}]

    class Done:
        stdout = json.dumps(listed)

    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: Done)
    found = cli.prs(["PMAT-282", "PMAT-283", "PMAT-284"], "/repo")
    assert found == {"PMAT-282": Pr(8, "OPEN", "u8"), "PMAT-283": Pr(10, "OPEN", "u10"), "PMAT-284": None}


def test_each_ticket_s_pr_is_looked_up_in_the_repo_its_worker_started_in(tmp_path, monkeypatch, capsys):
    # One orchestrator, tickets from two repos: each ticket's PR comes from its own repo folder, and a failing
    # lookup in one repo leaves the other's PRs on screen.
    monkeypatch.setattr(status, "DIR", tmp_path)
    for key, folder in (("PMAT-1", "/repos/a"), ("PMAT-2", "/repos/b"), ("PMAT-3", "/repos/a")):
        status.update(key, ticket=key, session_id="s", launch_dir=folder)

    async def snapshot(keys):
        return {}

    monkeypatch.setattr(cli.jira, "snapshot", snapshot)
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=lambda: {"PMAT-1", "PMAT-2", "PMAT-3"}, new_window=None,
                                             send=None, kill=None))
    calls = []

    def prs(keys, repo_dir):
        calls.append((sorted(keys), repo_dir))
        if repo_dir == "/repos/b":
            raise RuntimeError("gh: not a git repository")
        return {k: Pr(int(k[-1]), "OPEN", f"https://github.com/o/a/pull/{k[-1]}") for k in keys}

    monkeypatch.setattr(cli, "prs", prs)
    assert cli.main([], once=True) == 0
    assert sorted(calls) == [(["PMAT-1", "PMAT-3"], "/repos/a"), (["PMAT-2"], "/repos/b")]
    out = capsys.readouterr().out
    assert "https://github.com/o/a/pull/1" in out and "https://github.com/o/a/pull/3" in out
    assert "PR lookup failed in /repos/b" in out


def test_starting_tickets_needs_their_repo_folder(tmp_path, monkeypatch, capsys):
    # No default folder: a ticket must never start in whatever folder the owner happens to be in.
    monkeypatch.setattr(status, "DIR", tmp_path)
    opened = []  # fakes, so a failing run never opens a real tab
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=set, new_window=lambda *a: opened.append(a), send=None,
                                             kill=None))
    with pytest.raises(SystemExit) as done:
        cli.main(["PMAT-1"], once=True)
    assert opened == []
    assert done.value.code == 2 and "--repo-dir is required" in capsys.readouterr().err
    assert status.active() == []


def test_a_ticket_closed_in_this_check_is_not_drawn_as_resuming(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(status, "DIR", tmp_path)
    status.update("PMAT-1", ticket="PMAT-1", session_id="s", launch_dir="/repos/a")

    async def snapshot(keys):
        return {}

    def closes(keys, tickets, ops):  # this check closes the finished ticket's tab
        status.update("PMAT-1", finished=True)
        return ["PMAT-1: Done, closed its tab"]

    monkeypatch.setattr(cli.jira, "snapshot", snapshot)
    monkeypatch.setattr(cli, "prs", lambda keys, repo_dir: {})
    monkeypatch.setattr(cli.loop, "tick", closes)
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=set, new_window=None, send=None, kill=None))
    assert cli.main([], once=True) == 0
    out = capsys.readouterr().out
    assert "PMAT-1: Done, closed its tab" in out and "resuming the worker" not in out


def test_only_jira_style_keys_are_started(tmp_path, monkeypatch, capsys):
    # A key becomes a tab name, part of the shell command tmux runs, and a status file name: "../x" would write
    # outside the status folder. Nothing starts unless every key looks like PROJECT-123.
    monkeypatch.setattr(status, "DIR", tmp_path)
    opened = []
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=lambda: {key for key, *_ in opened},
                                             new_window=lambda *a: opened.append(a), send=None, kill=None))
    for bad in ("../x", "PMAT-1;rm -rf ~", "pmat-1", "PMAT"):
        with pytest.raises(SystemExit) as done:
            cli.main([bad, "--repo-dir", str(tmp_path)], once=True)
        assert done.value.code == 2 and "not a Jira key" in capsys.readouterr().err
    assert opened == [] and status.active() == []


def test_once_is_not_a_command_line_option(capsys):
    with pytest.raises(SystemExit) as done:
        cli.main(["--once"])
    assert done.value.code == 2


def test_a_second_copy_refuses_to_start(tmp_path, monkeypatch, capsys):
    import fcntl
    monkeypatch.setattr(status, "DIR", tmp_path)
    with open(tmp_path / "orchestrator.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)  # the first copy
        assert cli.main([], once=True) == 1
    assert "already running" in capsys.readouterr().err


def test_in_a_terminal_the_header_counts_down_to_the_next_check(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(status, "DIR", tmp_path)
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=set, new_window=None, send=None, kill=None))
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise KeyboardInterrupt  # stop after one countdown

    monkeypatch.setattr(cli.time, "sleep", sleep)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["--interval", "3"])
    out = capsys.readouterr().out
    assert sleeps == [1, 1, 1]
    assert out.index("next check in 3s") < out.index("next check in 2s") < out.index("next check in 1s")


def test_help_explains_use_and_does_not_start_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(status, "DIR", tmp_path)
    with pytest.raises(SystemExit) as done:
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert done.value.code == 0 and list(tmp_path.iterdir()) == []  # no lock, no status files
    for text in ("examples:", "mesh orchestrate --repo-dir ~/environment/pmat/alpha-mesh PMAT-282 PMAT-283", "Ctrl-C", "MESSAGE-FROM-WORKER", "events.log",
                 "--repo-dir DIR"):
        assert text in out


def test_a_jira_check_that_never_returns_is_cut_off_and_retried_next_check(tmp_path, monkeypatch, capsys):
    # PMAT-306: an MCP call that never answers froze the whole loop. The check now has a deadline: tmux-only work
    # (resuming a missing tab) still happens, nothing Jira-driven does, and the next check tries Jira again.
    import asyncio
    import threading
    monkeypatch.setattr(status, "DIR", tmp_path)
    monkeypatch.setattr(cli, "JIRA_DEADLINE", 0.2)
    monkeypatch.setattr(cli.jira, "logged_in", lambda: True)
    status.update("PMAT-1", ticket="PMAT-1", session_id="s1", launch_dir="/repos/a", relayed_comment="c1",
                  state="idle")
    status.update("PMAT-2", ticket="PMAT-2", session_id="s2", launch_dir="/repos/a")
    answers = [None, {"PMAT-1": jira.Ticket(labels=[], done=False, comment_id="c2")}]

    async def snapshot(keys):
        if (answer := answers.pop(0)) is None:
            await asyncio.Event().wait()  # Atlassian never replies
        return answer

    tabs, sent, after_check = {"PMAT-1"}, [], []
    monkeypatch.setattr(cli.jira, "snapshot", snapshot)
    monkeypatch.setattr(cli, "prs", lambda keys, repo_dir: {})
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=lambda: set(tabs), new_window=lambda key, *a: tabs.add(key),
                                             send=lambda key, text: sent.append((key, text)), kill=None))

    def sleep(seconds):  # between checks; stop after the second
        after_check.append((set(tabs), list(sent), capsys.readouterr().out))
        if len(after_check) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", sleep)

    def run():
        try:
            cli.main([])
        except KeyboardInterrupt:
            pass

    t = threading.Thread(target=run, daemon=True)  # guarded: before the fix the first check hangs for ever
    t.start()
    t.join(5)
    assert not t.is_alive(), "the Jira check hung past its deadline"
    (tabs1, sent1, out1), (_, sent2, _) = after_check
    assert "Jira check timed out, retrying next check" in out1
    assert tabs1 == {"PMAT-1", "PMAT-2"} and sent1 == []  # missing tab resumed, no Jira-driven action
    assert sent2 == [("PMAT-1", loop.REPLY.format(key="PMAT-1", after="c1", comment="c2"))]  # Jira answered


def test_max_workers_caps_running_workers_and_queues_the_rest(tmp_path, monkeypatch, capsys):
    # PMAT-298: 4 tickets, --max-workers 3: 3 tabs open, the 4th waits in the Queue box. When one of the 3 closes
    # as Done, the 4th starts on the next check. Each cli.main call is a restart, so the cap survives one too.
    from datetime import datetime, timedelta, timezone
    monkeypatch.setattr(status, "DIR", tmp_path / "status")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    tabs, opened = set(), []

    def new_window(key, cwd, cmd):
        tabs.add(key)
        opened.append(key)

    tickets = {}

    async def snapshot(keys):
        return {k: tickets.get(k, jira.Ticket(labels=[], done=False, comment_id=None)) for k in keys}

    monkeypatch.setattr(cli.jira, "snapshot", snapshot)
    monkeypatch.setattr(cli.jira, "logged_in", lambda: True)
    monkeypatch.setattr(cli, "prs", lambda keys, repo_dir: {})
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=lambda: set(tabs), new_window=new_window, send=lambda *a: None,
                                             kill=tabs.discard))

    assert cli.main(["PMAT-1", "PMAT-2", "PMAT-3", "PMAT-4", "--repo-dir", str(repo), "--max-workers", "3"],
                    once=True) == 0
    out = capsys.readouterr().out
    assert opened == ["PMAT-1", "PMAT-2", "PMAT-3"]
    assert "Queue" in out and "1. PMAT-4" in out

    # A restart keeps the cap: the 3 running workers count toward it, PMAT-4 stays queued, and naming it again
    # does not queue it twice.
    assert cli.main(["PMAT-4", "--repo-dir", str(repo)], once=True) == 0
    out = capsys.readouterr().out
    assert opened == ["PMAT-1", "PMAT-2", "PMAT-3"] and "PMAT-4: already queued" in out
    assert status.queued() == [("PMAT-4", str(repo))] and "1. PMAT-4" in out

    # PMAT-1 is Done and its close warning has run out: this check closes its tab...
    long_ago = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
    status.update("PMAT-1", state="idle", close_warned_at=long_ago)
    tickets["PMAT-1"] = jira.Ticket(labels=[], done=True, comment_id=None)
    assert cli.main([], once=True) == 0
    assert "PMAT-1" not in tabs and opened == ["PMAT-1", "PMAT-2", "PMAT-3"]
    capsys.readouterr()
    # ...and the next check starts the queued ticket, in the repo it was named for.
    assert cli.main([], once=True) == 0
    out = capsys.readouterr().out
    assert opened[-1] == "PMAT-4" and status.read("PMAT-4")["launch_dir"] == str(repo)
    assert "Queue" not in out


def test_max_workers_must_be_at_least_one(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(status, "DIR", tmp_path)
    with pytest.raises(SystemExit) as done:
        cli.main(["--max-workers", "0"], once=True)
    assert done.value.code == 2 and "--max-workers" in capsys.readouterr().err


def test_a_queued_ticket_that_fails_to_start_does_not_stop_the_orchestrator(tmp_path, monkeypatch, capsys):
    # R36-812f904-002: the first start, before the loop, must be guarded like the ones inside it.
    monkeypatch.setattr(status, "DIR", tmp_path / "status")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    def refused(*args):
        raise RuntimeError("tmux refused the tab")

    async def snapshot(keys):
        return {}

    monkeypatch.setattr(cli.jira, "snapshot", snapshot)
    monkeypatch.setattr(cli.jira, "logged_in", lambda: True)
    monkeypatch.setattr(cli, "prs", lambda keys, repo_dir: {})
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=set, new_window=refused, send=None, kill=None))
    assert cli.main(["PMAT-1", "--repo-dir", str(repo)], once=True) == 0
    assert "Starting a queued ticket failed" in capsys.readouterr().out
    # R36-d2309a5-001: it goes back to the front of the queue, not active, so it is not resumed as a session that
    # never started; the next check starts it afresh.
    assert status.active() == [] and status.queued() == [("PMAT-1", str(repo))]
    opened = []
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=lambda: {key for key, *_ in opened},
                                             new_window=lambda *a: opened.append(a), send=None, kill=None))
    assert cli.main([], once=True) == 0
    assert [(key, cwd) for key, cwd, _ in opened] == [("PMAT-1", str(repo))]
    assert "'Work on Jira PMAT-1.'" in opened[0][2] and status.queued() == []


def test_a_worker_that_lost_its_jira_connector_is_flagged_until_jira_works_again(tmp_path, monkeypatch, capsys):
    # PMAT-303: the worker marks the loss itself (jira-access.sh lost, per worker-prompt.md); the PostToolUse hook
    # clears it on the next working Atlassian call (jira-access.sh ok). Both run the real script.
    monkeypatch.setattr(status, "DIR", tmp_path / "environment/logs/orchestrator")
    status.update("PMAT-300", ticket="PMAT-300", session_id="s", launch_dir="/repos/a", relayed_comment="c1",
                  state="idle")

    async def snapshot(keys):
        return {"PMAT-300": jira.Ticket(labels=["ALPHA-MESH"], done=False, comment_id="c1")}

    monkeypatch.setattr(cli.jira, "snapshot", snapshot)
    monkeypatch.setattr(cli, "prs", lambda keys, repo_dir: {})
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=lambda: {"PMAT-300"}, new_window=None, send=None, kill=None))

    def access(what):
        subprocess.run([str(loop.AGENT_DIR / "jira-access.sh"), what], check=True, input="{}", text=True,
                       env={**os.environ, "HOME": str(tmp_path), "ORCH_TICKET": "PMAT-300"})

    warning = 'Jira lost: in tab PMAT-300 run /mcp (may need a browser login), then type "continue"'
    access("lost")
    assert cli.main([], once=True) == 0
    assert warning in capsys.readouterr().out
    access("ok")
    assert cli.main([], once=True) == 0
    assert warning not in capsys.readouterr().out


def test_the_jira_access_hook_matches_both_atlassian_connectors_but_not_their_login_tools():
    # Claude Code reads a matcher with other characters than letters, digits, _, -, | as an unanchored JavaScript
    # regex; this one uses nothing Python's re reads differently.
    hooks = json.loads((loop.AGENT_DIR / "settings.json").read_text())["hooks"]["PostToolUse"]
    matcher = next(h["matcher"] for h in hooks if "jira-access.sh ok" in h["hooks"][0]["command"])
    for name in ("mcp__plugin_atlassian_atlassian__getJiraIssue", "mcp__atlassian__addCommentToJiraIssue",
                 "mcp__plugin_atlassian_atlassian__executeRead"):
        assert re.search(matcher, name), name
    for name in ("mcp__plugin_atlassian_atlassian__authenticate", "mcp__atlassian__complete_authentication",
                 "mcp__claude_ai_Gmail__authenticate", "Bash", "ToolSearch"):
        assert not re.search(matcher, name), name
