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
    assert cli.main(["--once"]) == 0
    assert "Tick failed, trying again next tick" in capsys.readouterr().out  # shown under Recent


def test_window_names_with_spaces_are_kept_whole(monkeypatch):
    monkeypatch.setattr(tmux, "_tmux", lambda *args: "PMAT-1 notes\nshell\n")
    assert tmux.windows() == {"PMAT-1 notes", "shell"}


def test_recent_events_survive_a_restart(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(status, "DIR", tmp_path)
    (tmp_path / "events.log").write_text("2026-10-22T15:15:20+00:00  PMAT-282: passed on the owner's comment 13103\n")
    monkeypatch.setattr(cli, "OPS", loop.Ops(windows=set, new_window=None, send=None, kill=None))
    assert cli.main(["--once"]) == 0
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


def test_a_second_copy_refuses_to_start(tmp_path, monkeypatch, capsys):
    import fcntl
    monkeypatch.setattr(status, "DIR", tmp_path)
    with open(tmp_path / "orchestrator.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)  # the first copy
        assert cli.main(["--once"]) == 1
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
    for text in ("examples:", "mesh orchestrate PMAT-282 PMAT-283", "Ctrl-C", "MESSAGE-FROM-WORKER", "events.log"):
        assert text in out
