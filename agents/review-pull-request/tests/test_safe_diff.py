"""SafeDiff: a diff that has been through the policy; the only diff type stages accept."""

import dataclasses

import pytest

from review_pull_request.policy import PathPolicy
from review_pull_request.safe_diff import SafeDiff

SECRET = "TOKEN=s3cret-value"


def _file(path, body):
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n+{body}\n"


RAW = _file(".env", SECRET) + _file("svc/.ENV", SECRET) + _file("src/a.py", "x = 1") + _file("README.md", "docs")


def test_text_and_chunks_contain_only_allowed_files():
    safe = SafeDiff.from_raw(RAW, PathPolicy(["**/.env", "**/README.md"]))

    assert [c.file_path for c in safe.chunks] == ["src/a.py"]
    assert "s3cret-value" not in safe.text
    assert "x = 1" in safe.text


def test_excluded_paths_are_recorded_in_diff_order():
    safe = SafeDiff.from_raw(RAW, PathPolicy(["**/.env", "**/README.md"]))

    assert safe.excluded == (".env", "svc/.ENV", "README.md")


def test_all_excluded_is_reported():
    safe = SafeDiff.from_raw(_file(".env", SECRET), PathPolicy(["**/.env"]))

    assert safe.chunks == () and safe.all_excluded is True


def test_empty_diff_is_not_reported_as_all_excluded():
    assert SafeDiff.from_raw("", PathPolicy(["**/.env"])).all_excluded is False


def test_safe_diff_is_immutable():
    safe = SafeDiff.from_raw(RAW, PathPolicy([]))

    with pytest.raises(dataclasses.FrozenInstanceError):
        safe.chunks = ()


def test_a_rename_out_of_an_excluded_file_is_excluded():
    diff = ("diff --git a/.env b/notes.md\nrename from .env\nrename to notes.md\n"
            "--- a/.env\n+++ b/notes.md\n@@ -1 +1 @@\n-TOKEN=x\n+y\n")
    safe = SafeDiff.from_raw(diff, PathPolicy(["**/.env"]))
    assert safe.chunks == ()
    assert safe.excluded == ("notes.md",)


def test_paths_with_spaces_are_read_whole():
    diff = "diff --git a/my dir/a.py b/my dir/a.py\n--- a/my dir/a.py\n+++ b/my dir/a.py\n@@ -1 +1 @@\n+x\n"
    safe = SafeDiff.from_raw(diff, PathPolicy([]))
    assert [c.file_path for c in safe.chunks] == ["my dir/a.py"]


def test_quoted_paths_are_unquoted_not_dropped():
    diff = ('diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n'
            '--- "a/caf\\303\\251.py"\n+++ "b/caf\\303\\251.py"\n@@ -1 +1 @@\n+x\n')
    safe = SafeDiff.from_raw(diff, PathPolicy([]))
    assert [c.file_path for c in safe.chunks] == ["café.py"]


def test_a_header_that_cannot_be_parsed_is_excluded_not_dropped_or_reviewed():
    diff = "diff --git garbage\n@@ -1 +1 @@\n+TOKEN=x\n"
    safe = SafeDiff.from_raw(diff, PathPolicy([]))
    assert safe.chunks == ()
    assert len(safe.excluded) == 1


def test_a_deleted_file_keeps_its_old_path():
    diff = "diff --git a/old.py b/old.py\ndeleted file mode 100644\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
    safe = SafeDiff.from_raw(diff, PathPolicy([]))
    assert [c.file_path for c in safe.chunks] == ["old.py"]


def test_a_binary_file_without_hunk_headers_is_still_named():
    diff = "diff --git a/img.png b/img.png\nBinary files a/img.png and b/img.png differ\n"
    safe = SafeDiff.from_raw(diff, PathPolicy([]))
    assert [c.file_path for c in safe.chunks] == ["img.png"]
