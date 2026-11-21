"""Acceptance tests for archiving eval runs to S3 (PMAT-336).

Offline, against a fake S3 client: prune keeps only the runs a committed file names (acceptance test 2), the archive
holds only results.json, run.json and traces/ with no path of this machine (3), and a run pulled into another folder
gives the judge the same files (1, offline part). ARCHIVE_LIVE=1 runs acceptance test 1 against the real bucket with
the stored base6 run: push, pull into another folder, then the same judge files for every review and the same
labelling sheet (no model is called).
"""

import io
import json
import os
import shutil
import socket
import tarfile
from pathlib import Path

import pytest

import agent_provider
import archive_runs
import write_baseline as baseline
import case_rules
import judge_sample
import llm_rubric
from review_pull_request.policy import PathPolicy

SHA = "a" * 40


class FakeS3:
    """The few S3 calls archive_runs makes, over a dict of key -> bytes."""

    def __init__(self, objects=None):
        self.objects = dict(objects or {})
        self.deleted = []

    def put_object(self, Bucket, Key, Body, **_):
        self.objects[Key] = Body if isinstance(Body, bytes) else Body.read()

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"Body": io.BytesIO(self.objects[Key])}

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        s3 = self

        class Paginator:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k} for k in sorted(s3.objects) if k.startswith(Prefix)]}
        return Paginator()

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        del self.objects[Key]


def make_run(root: Path, name="PMAT-1-base1") -> Path:
    """A run folder as run_eval.sh leaves it: absolute trace paths, plus state the archive must leave out."""
    run = root / name
    trace = run / "traces" / "repeat-0" / "abc.jsonl"
    trace.parent.mkdir(parents=True)
    trace.write_text('{"span": "review"}\n')
    output = {"head_sha": SHA, "model": "m", "files": [], "incomplete": [], "excluded": [], "findings": [],
              "trace": str(trace.resolve())}
    results = {"evalId": "e", "config": {"basePath": str(root / "worktree" / "eval"),
                                         "outputPath": [str(run / "results.json")]},
               "results": {"results": [{"vars": {"case_id": "C1"}, "repeatIndex": 0, "failureReason": 0,
                                        "response": {"output": output}}]}}
    (run / "results.json").write_text(json.dumps(results))
    (run / "run.json").write_text(json.dumps({"commit": SHA, "model": "m", "date": "2026-11-13", "k": 1,
                                              "window": 3, "dirty": False}))
    (run / "checkpoints-repeat-0.sqlite").write_bytes(b"sqlite")
    (run / "locks").mkdir()
    (run / "locks" / "abc-repeat-0.lock").write_text("")
    (run / ".promptfoo").mkdir()
    (run / ".promptfoo" / "promptfoo.db").write_bytes(b"db")
    return run


def members(archive: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}


# 3: what's in the archive ─────────────────────────────────────────────────────────────────────────

def test_the_archive_holds_only_results_run_and_traces_with_no_machine_path(tmp_path):
    run = make_run(tmp_path)

    archive = archive_runs.pack(run)

    files = members(archive)
    assert set(files) == {"results.json", "run.json", "traces/repeat-0/abc.jsonl"}
    for name, content in files.items():
        text = content.decode()
        assert str(tmp_path) not in text and str(Path.home()) not in text, name
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        assert {(m.uname, m.gname, m.uid, m.gid) for m in tar.getmembers()} == {("", "", 0, 0)}
    output = json.loads(files["results.json"])["results"]["results"][0]["response"]["output"]
    assert output["trace"] == "traces/repeat-0/abc.jsonl"


def test_push_refuses_a_run_whose_results_still_name_this_machine(tmp_path):
    """A path left in results.json (one that isn't a trace or promptfoo's config) stops the push, not leaks."""
    run = make_run(tmp_path)
    results = json.loads((run / "results.json").read_text())
    results["results"]["results"][0]["error"] = f"failed reading {run}/elsewhere"
    (run / "results.json").write_text(json.dumps(results))

    with pytest.raises(archive_runs.ArchiveError, match="names this machine"):
        archive_runs.pack(run)


def test_push_refuses_a_folder_that_is_not_a_run(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(archive_runs.ArchiveError, match="results.json"):
        archive_runs.pack(tmp_path / "empty")


# 1 (offline): round trip ───────────────────────────────────────────────────────────────────────────

def test_a_run_pulled_into_another_folder_gives_the_judge_the_same_files(tmp_path):
    run = make_run(tmp_path / "here")
    s3 = FakeS3()
    archive_runs.push(run, s3, "bucket")
    assert set(s3.objects) == {"eval-runs/PMAT-1-base1.tar.gz"}

    pulled = archive_runs.pull("PMAT-1-base1", s3, "bucket", tmp_path / "elsewhere" / "copy")

    agent_provider._stored_rows.cache_clear()
    original = agent_provider._stored_review(str(run / "results.json"), "C1", 0)
    copy = agent_provider._stored_review(str(pulled / "results.json"), "C1", 0)
    assert Path(copy["trace"]) == pulled / "traces" / "repeat-0" / "abc.jsonl"
    policy = PathPolicy([])
    assert llm_rubric.traced_files(copy, policy) == llm_rubric.traced_files(original, policy)
    assert (pulled / "run.json").read_text() == (run / "run.json").read_text()


def test_pull_refuses_to_overwrite_a_folder_with_files(tmp_path):
    s3 = FakeS3()
    archive_runs.push(make_run(tmp_path / "here"), s3, "bucket")
    target = tmp_path / "taken"
    target.mkdir()
    (target / "x").write_text("mine")

    with pytest.raises(archive_runs.ArchiveError, match="not empty"):
        archive_runs.pull("PMAT-1-base1", s3, "bucket", target)
    assert (target / "x").read_text() == "mine"


def test_pull_refuses_an_archive_member_outside_the_run(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"x"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    s3 = FakeS3({"eval-runs/bad.tar.gz": buf.getvalue()})

    with pytest.raises(archive_runs.ArchiveError, match="escape.txt"):
        archive_runs.pull("bad", s3, "bucket", tmp_path / "copy")
    assert not (tmp_path / "escape.txt").exists()


def test_an_absolute_trace_still_loads_as_it_was(tmp_path):
    """Runs on this machine keep their absolute trace paths: the loader leaves them alone."""
    run = make_run(tmp_path)
    agent_provider._stored_rows.cache_clear()
    review = agent_provider._stored_review(str(run / "results.json"), "C1", 0)
    assert review["trace"] == str((run / "traces" / "repeat-0" / "abc.jsonl").resolve())


# Security review (owner, PR #63): no path outside the run is uploaded, written or read ─────────────

def test_push_refuses_a_symlink_in_traces_so_no_file_outside_the_run_is_uploaded(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("CANARY-OUTSIDE-THE-RUN")
    run = make_run(tmp_path)
    (run / "traces" / "repeat-0" / "link.jsonl").symlink_to(secret)
    s3 = FakeS3()

    with pytest.raises(archive_runs.ArchiveError, match="link.jsonl"):
        archive_runs.push(run, s3, "bucket")
    assert s3.objects == {}


@pytest.mark.parametrize("name", ["../escape", "a/b", ".", ""])
def test_pull_refuses_a_run_name_that_is_not_a_plain_folder_name(tmp_path, name):
    s3 = FakeS3()
    with pytest.raises(archive_runs.ArchiveError, match="run name"):
        archive_runs.pull(name, s3, "bucket", tmp_path / "copy")
    assert not (tmp_path / "copy").exists()


def test_a_relative_trace_outside_the_run_folder_is_refused(tmp_path):
    """A pulled results.json can't make the judge read a file outside its run."""
    run = make_run(tmp_path)
    results = json.loads((run / "results.json").read_text())
    results["results"]["results"][0]["response"]["output"]["trace"] = "../../outside.jsonl"
    (run / "results.json").write_text(json.dumps(results))
    agent_provider._stored_rows.cache_clear()

    with pytest.raises(LookupError, match="outside"):
        agent_provider._stored_review(str(run / "results.json"), "C1", 0)


# 2: prune keeps only what's needed ─────────────────────────────────────────────────────────────────

def needed():
    return {json.loads(baseline.BASELINE.read_text())["run"], json.loads(judge_sample.SAMPLE.read_text())["run"]}


def three_runs():
    return FakeS3({f"eval-runs/{name}.tar.gz": b"x" for name in [*sorted(needed()), "PMAT-1-replaced"]})


def test_the_committed_baseline_and_sample_name_their_runs():
    assert needed() == {"PMAT-325-base6-nemotron", "PMAT-322-base3"}
    assert archive_runs.needed_runs() == needed()


def test_prune_deletes_exactly_the_run_no_committed_file_names():
    s3 = three_runs()

    archive_runs.prune(s3, "bucket", yes=True)

    assert s3.deleted == ["eval-runs/PMAT-1-replaced.tar.gz"]
    assert set(s3.objects) == {f"eval-runs/{n}.tar.gz" for n in needed()}


def test_a_dry_run_prune_deletes_nothing(capsys):
    s3 = three_runs()

    archive_runs.prune(s3, "bucket", yes=False)

    assert s3.deleted == []
    assert "PMAT-1-replaced" in capsys.readouterr().out


def test_prune_refuses_while_a_needed_run_is_not_in_the_bucket():
    """Never prune toward an archive that lacks what the committed files depend on (push it first)."""
    s3 = FakeS3({"eval-runs/PMAT-325-base6-nemotron.tar.gz": b"x", "eval-runs/PMAT-1-replaced.tar.gz": b"x"})

    with pytest.raises(archive_runs.ArchiveError, match="PMAT-322-base3"):
        archive_runs.prune(s3, "bucket", yes=True)
    assert s3.deleted == []


def test_prune_leaves_keys_outside_the_runs_prefix_alone():
    s3 = three_runs()
    s3.objects["eval-runs/notes.txt"] = b"x"

    archive_runs.prune(s3, "bucket", yes=True)

    assert "eval-runs/notes.txt" in s3.objects


# The baseline names its run ─────────────────────────────────────────────────────────────────────────

def test_a_new_baseline_names_the_run_it_was_built_from(tmp_path, monkeypatch):
    run = tmp_path / "PMAT-9-base9"
    run.mkdir()
    (run / "run.json").write_text(json.dumps({"commit": SHA, "model": "m", "date": "2026-11-13", "k": 1,
                                              "window": 3, "dirty": False}))
    (run / "results.json").write_text(json.dumps({"results": {"results": []}}))
    link = tmp_path / "eval-runs-link"
    link.symlink_to(run)  # eval/runs/<run> is often a link to ~/.mesh/eval-runs/<run>: the archive uses the real name
    monkeypatch.setattr(baseline, "BASELINE", tmp_path / "baseline.json")
    monkeypatch.setattr(baseline, "ANALYSIS", tmp_path / "error_analysis.json")
    monkeypatch.setattr(baseline, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(baseline, "label_changes", lambda *a: [])
    monkeypatch.setattr(case_rules, "development_cases", lambda: [])
    monkeypatch.setattr(baseline.subprocess, "run",
                        lambda *a, **k: type("Done", (), {"returncode": 0, "stdout": SHA if "rev-parse" in a[0] else "",
                                                          "stderr": ""})())

    assert baseline.main([str(link)]) == 0
    assert json.loads((tmp_path / "baseline.json").read_text())["run"] == "PMAT-9-base9"


# 1 (live): base6 through the real bucket ───────────────────────────────────────────────────────────

BASE6 = Path.home() / ".mesh" / "eval-runs" / "PMAT-325-base6-nemotron"


@pytest.mark.skipif(os.environ.get("ARCHIVE_LIVE") != "1", reason="ARCHIVE_LIVE=1: real S3 and the stored base6 run")
def test_live_base6_round_trip(tmp_path, monkeypatch):
    s3, bucket = archive_runs.client(), archive_runs.bucket_name()
    archive_runs.push(BASE6, s3, bucket)
    pulled = archive_runs.pull(BASE6.name, s3, bucket, tmp_path / "elsewhere" / BASE6.name)

    archived = members(s3.get_object(Bucket=bucket, Key=f"eval-runs/{BASE6.name}.tar.gz")["Body"].read())
    assert {n for n in archived if not n.startswith("traces/")} == {"results.json", "run.json"}
    for name, content in archived.items():
        text = content.decode()
        assert "/home/" not in text and socket.gethostname() not in text, name

    # run_eval.sh --stored accepts the folder: it needs results.json and run.json
    assert (pulled / "results.json").is_file() and (pulled / "run.json").is_file()

    # The judge's files from the trace, for every stored review
    agent_provider._stored_rows.cache_clear()
    policy = PathPolicy(llm_rubric.judge_config().exclusions)
    rows = agent_provider._stored_rows(str(BASE6 / "results.json"))
    compared = 0
    for case_id, repeat in rows:
        original = agent_provider._stored_review(str(BASE6 / "results.json"), case_id, repeat)
        copy = agent_provider._stored_review(str(pulled / "results.json"), case_id, repeat)
        if original["trace"] is None:  # 3 reviews read no file, so they have no trace
            assert copy == original
            continue
        assert copy["trace"].startswith(str(pulled))
        assert llm_rubric.traced_files(copy, policy) == llm_rubric.traced_files(original, policy), (case_id, repeat)
        compared += 1
    assert compared == 192

    # The labelling sheet built from each copy
    import judge_labelling_sheet
    monkeypatch.setattr(judge_labelling_sheet, "LABELS", tmp_path / "labels.json")
    sheets = {}
    for label, folder in (("original", BASE6), ("pulled", pulled)):
        monkeypatch.setattr(judge_labelling_sheet, "OUT", tmp_path / f"sheet-{label}.md")
        assert judge_labelling_sheet.main([str(folder)]) == 0
        sheets[label] = (tmp_path / f"sheet-{label}.md").read_text()
    assert sheets["original"] == sheets["pulled"]
    shutil.rmtree(tmp_path / "elsewhere")


def test_with_no_aws_region_anywhere_the_archive_stops_with_a_clear_error(monkeypatch, tmp_path, capsys):
    # PMAT-347: no region is committed, so none is guessed.
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    (tmp_path / "aws-config").write_text("[profile zz]\n")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws-config"))
    monkeypatch.setenv("AWS_PROFILE", "zz")
    assert archive_runs.main(["prune"]) == 1
    assert "no AWS region: set AWS_REGION" in capsys.readouterr().err
