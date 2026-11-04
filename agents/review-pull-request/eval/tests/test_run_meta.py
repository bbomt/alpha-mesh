"""run.json across resumes (PMAT-304): a run keeps the commit it started at, or label fixes made between passes
would not be counted as label_errors_fixed."""

import json

import pytest

import run_meta


def meta(path):
    return json.loads(path.read_text())


def test_a_new_run_records_its_commit(tmp_path):
    path = tmp_path / "run.json"
    run_meta.write(path, commit="c1", model="m", date="2026-10-30", k=3, dirty=False, changed=lambda since: False)
    assert meta(path) == {"commit": "c1", "model": "m", "date": "2026-10-30", "k": 3, "window": 3, "dirty": False}


def test_a_resumed_run_keeps_the_commit_and_date_it_started_at(tmp_path):
    path = tmp_path / "run.json"
    run_meta.write(path, commit="c1", model="m", date="2026-10-30", k=3, dirty=False, changed=lambda since: False)
    run_meta.write(path, commit="c2", model="m", date="2026-11-03", k=3, dirty=False, changed=lambda since: False)
    assert (meta(path)["commit"], meta(path)["date"]) == ("c1", "2026-10-30")


def test_a_resume_after_the_agent_changed_is_refused(tmp_path):
    path = tmp_path / "run.json"
    run_meta.write(path, commit="c1", model="m", date="2026-10-30", k=3, dirty=False, changed=lambda since: False)
    with pytest.raises(SystemExit, match="changed since"):
        run_meta.write(path, commit="c2", model="m", date="2026-11-03", k=3, dirty=False,
                       changed=lambda since: since == "c1")


def test_a_resume_with_another_model_or_k_is_refused(tmp_path):
    path = tmp_path / "run.json"
    run_meta.write(path, commit="c1", model="m", date="2026-10-30", k=3, dirty=False, changed=lambda since: False)
    with pytest.raises(SystemExit, match="another model"):
        run_meta.write(path, commit="c1", model="other", date="d", k=3, dirty=False, changed=lambda since: False)
    with pytest.raises(SystemExit, match="k="):
        run_meta.write(path, commit="c1", model="m", date="d", k=1, dirty=False, changed=lambda since: False)


def test_a_dirty_pass_marks_the_whole_run_dirty(tmp_path):
    path = tmp_path / "run.json"
    run_meta.write(path, commit="c1", model="m", date="d", k=3, dirty=True, changed=lambda since: False)
    run_meta.write(path, commit="c1", model="m", date="d", k=3, dirty=False, changed=lambda since: False)
    assert meta(path)["dirty"] is True


def test_a_commit_that_looks_like_a_git_option_writes_nothing(tmp_path):
    """run.json's commit reaches `git diff`; `--output=<file>` would overwrite that file."""
    target = tmp_path / "written-by-git"
    with pytest.raises(SystemExit):
        run_meta.agent_changed(f"--output={target}")
    assert not target.exists()
