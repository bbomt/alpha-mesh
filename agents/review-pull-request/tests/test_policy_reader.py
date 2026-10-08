"""PolicyFileReader: a decorator that refuses excluded paths before any read happens."""

import os

from review_pull_request.policy import PathPolicy
from review_pull_request.readers import LocalFileReader, PolicyFileReader, ReadError
from tests.helpers import StubReader


def _guarded(inner, patterns=("**/.env",)):
    return PolicyFileReader(inner, PathPolicy(list(patterns)))


def test_excluded_path_is_refused_and_never_read():
    inner = StubReader(default="TOKEN=secret")

    result = _guarded(inner).read_file(".env", 100)

    assert isinstance(result, ReadError) and "excluded" in result
    assert inner.asked == []


def test_path_tricks_and_case_variants_are_refused():
    inner = StubReader(default="TOKEN=secret")
    reader = _guarded(inner)

    for sneaky in ("src/../.env", "./.env", "/.env", ".ENV"):
        assert isinstance(reader.read_file(sneaky, 100), ReadError), sneaky
    assert inner.asked == []


def test_allowed_paths_are_passed_through():
    inner = StubReader({"src/a.py": "print(1)\n"})

    assert _guarded(inner).read_file("src/a.py", 100) == "print(1)\n"


def test_symlink_to_an_excluded_file_is_refused(tmp_path):
    (tmp_path / ".env").write_text("SECRET=1")
    (tmp_path / "notes").mkdir()
    os.symlink(tmp_path / ".env", tmp_path / "notes" / "link")

    result = _guarded(LocalFileReader(str(tmp_path))).read_file("notes/link", 100)

    assert "SECRET" not in result


def test_local_reader_reads_plain_files_and_reports_errors(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n")
    reader = LocalFileReader(str(tmp_path))

    assert reader.read_file("a.py", 100) == "x = 1\n"
    assert isinstance(reader.read_file("missing.py", 100), ReadError)
    assert isinstance(reader.read_file("../outside.py", 100), ReadError)
