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
