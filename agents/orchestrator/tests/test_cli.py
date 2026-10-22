"""The loop around the ticks, and the real tmux wrapper's parsing."""

from orchestrator import cli, loop, status, tmux


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
