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


def test_malformed_paths_from_the_model_are_errors_not_exceptions():
    reader = _guarded(StubReader(default="x"))

    for bad in ("a\x00b", 42, None, ["a"]):
        assert isinstance(reader.read_file(bad, 100), ReadError), repr(bad)


def test_local_reader_stops_reading_at_the_line_limit(tmp_path, monkeypatch):
    from review_pull_request import readers

    (tmp_path / "big.txt").write_text("".join(f"line {i}\n" for i in range(10_000)))
    consumed = []
    real_open = open

    class Counting:
        """A text file that counts the lines pulled from it (and has no readlines())."""
        def __init__(self, f):
            self.f = f
        def __enter__(self):
            return self
        def __exit__(self, *a):
            self.f.close()
        def __iter__(self):
            return self
        def __next__(self):
            consumed.append(1)
            return next(self.f)
        def read(self, n=-1):
            return self.f.read(n)

    monkeypatch.setattr(readers, "open", lambda *a, **k: Counting(real_open(*a, **k)), raising=False)

    text = LocalFileReader(str(tmp_path)).read_file("big.txt", 5)

    assert "TRUNCATED" in text and "line 4\n" in text and "line 5\n" not in text
    assert len(consumed) <= 6


def test_a_text_file_with_a_multibyte_character_at_the_sniff_boundary_is_not_binary(tmp_path):
    (tmp_path / "t.txt").write_bytes(b"a" * 8191 + "é".encode() + b"\nend\n")  # é straddles byte 8192

    text = LocalFileReader(str(tmp_path)).read_file("t.txt", 100)

    assert not isinstance(text, ReadError) and text.endswith("end\n")


def test_local_reader_caps_bytes_as_well_as_lines(tmp_path):
    from review_pull_request.readers import MAX_READ_BYTES

    (tmp_path / "min.js").write_text("x" * (MAX_READ_BYTES * 3) + "\n")  # one enormous line

    text = LocalFileReader(str(tmp_path)).read_file("min.js", 100)

    assert len(text) < MAX_READ_BYTES + 500 and "TRUNCATED" in text
