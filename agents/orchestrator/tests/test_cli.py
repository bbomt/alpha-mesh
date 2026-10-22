"""The loop around the ticks, and the real tmux wrapper's parsing."""

import json

from orchestrator import cli, loop, status, tmux
from orchestrator.display import Pr


def test_a_failing_tick_does_not_stop_the_orchestrator(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(status, "DIR", tmp_path)

    def broken(*args):
        raise RuntimeError("can't find window: PMAT-1")

    monkeypatch.setattr(loop, "tick", broken)
    assert cli.main(["--once"]) == 0
    assert "Tick failed" in capsys.readouterr().err


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
