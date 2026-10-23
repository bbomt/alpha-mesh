"""The loop around the ticks, and the real tmux wrapper's parsing."""

import json

import pytest

from orchestrator import cli, loop, status, tmux
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
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=set, new_window=lambda *a: opened.append(a), send=None,
                                             kill=None))
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
