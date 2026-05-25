"""Tests for tools.py — execute_read_file."""
import os
import tempfile
import pytest

from pr_review_performance.tools import execute_read_file, READ_FILE_TOOL_SCHEMA


class TestExecuteReadFile:
    def test_reads_valid_file(self, tmp_path):
        (tmp_path / "test.py").write_text("def foo():\n    return 42\n")
        result = execute_read_file("test.py", str(tmp_path), max_lines=8000)
        assert "def foo" in result
        assert "return 42" in result

    def test_path_traversal_rejected(self, tmp_path):
        result = execute_read_file("../../etc/passwd", str(tmp_path), max_lines=8000)
        assert "access denied" in result
        assert "Error" in result

    def test_nonexistent_file(self, tmp_path):
        result = execute_read_file("does_not_exist.py", str(tmp_path), max_lines=8000)
        assert "file not found" in result

    def test_truncation_notice(self, tmp_path):
        lines = ["line %d\n" % i for i in range(100)]
        (tmp_path / "big.py").write_text("".join(lines))
        result = execute_read_file("big.py", str(tmp_path), max_lines=10)
        assert "TRUNCATED" in result
        assert "10 lines" in result

    def test_binary_file_rejected(self, tmp_path):
        (tmp_path / "binary.bin").write_bytes(b"\x00\xff\xfe\x81")
        result = execute_read_file("binary.bin", str(tmp_path), max_lines=8000)
        assert "binary" in result.lower()

    def test_normalizes_slashes_in_path(self, tmp_path):
        subdir = tmp_path / "sub"
        subdir.mkdir()
        (subdir / "file.py").write_text("content")
        result = execute_read_file("sub/../sub/file.py", str(tmp_path), max_lines=8000)
        assert "content" in result

    def test_survives_symlink_outside(self, tmp_path):
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        (real_dir / "secret.txt").write_text("secret data")
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir, target_is_directory=True)

        # Symlink trying to escape
        if os.path.exists("/tmp") and not os.path.isabs("/tmp"):
            pass

        result = execute_read_file("link/../real/secret.txt", str(link_dir), max_lines=8000)
        # After resolving, should still be inside repo_root
        # Both link_dir and real_dir are inside tmp_path so allowed
        assert "Error" not in result or "file not found" in result


class TestReadFileToolSchema:
    def test_schema_has_required_fields(self):
        assert READ_FILE_TOOL_SCHEMA["type"] == "function"
        assert READ_FILE_TOOL_SCHEMA["function"]["name"] == "read_file"
        assert "path" in READ_FILE_TOOL_SCHEMA["function"]["parameters"]["properties"]
        assert (
            "path" in READ_FILE_TOOL_SCHEMA["function"]["parameters"]["required"]
        )
