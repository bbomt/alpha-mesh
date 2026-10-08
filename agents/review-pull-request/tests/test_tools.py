"""Tests for tools.py — execute_read_file and GitHubFileReader."""
import os
from unittest.mock import MagicMock, patch
import pytest

from review_pull_request.tools import execute_read_file, GitHubFileReader, READ_FILE_TOOL_SCHEMA


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


def _make_http_response(status_code: int, text: str = "", content: bytes = b"") -> MagicMock:
    """Build a minimal mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = content if content else text.encode("utf-8")
    resp.headers = {}
    resp.json.return_value = {}
    return resp


class TestGitHubFileReader:
    def _reader(self):
        return GitHubFileReader("owner", "repo", "abc123sha", "token123")

    @patch("review_pull_request.tools.httpx.Client")
    def test_fetches_file_content(self, MockClient):
        content = "def hello():\n    return 1\n"
        MockClient.return_value.get.return_value = _make_http_response(200, text=content)

        reader = self._reader()
        result = reader.read_file("src/hello.py", max_lines=8000)

        assert result == content
        MockClient.return_value.get.assert_called_once()
        call_url = MockClient.return_value.get.call_args[0][0]
        assert "owner/repo/contents/src/hello.py" in call_url
        assert "ref=abc123sha" in call_url

    @patch("review_pull_request.tools.httpx.Client")
    def test_caches_response(self, MockClient):
        content = "cached content"
        MockClient.return_value.get.return_value = _make_http_response(200, text=content)

        reader = self._reader()
        result1 = reader.read_file("src/foo.py", max_lines=8000)
        result2 = reader.read_file("src/foo.py", max_lines=8000)

        assert result1 == content
        assert result2 == content
        # Should only make one HTTP request
        assert MockClient.return_value.get.call_count == 1

    @patch("review_pull_request.tools.httpx.Client")
    def test_returns_error_on_404(self, MockClient):
        MockClient.return_value.get.return_value = _make_http_response(404)

        reader = self._reader()
        result = reader.read_file("missing.py", max_lines=8000)

        assert "file not found" in result
        assert "Error" in result

    @patch("review_pull_request.tools.httpx.Client")
    def test_returns_error_on_rate_limit_429(self, MockClient):
        MockClient.return_value.get.return_value = _make_http_response(429)

        reader = self._reader()
        result = reader.read_file("any.py", max_lines=8000)

        assert "rate limit" in result.lower()
        assert "Error" in result

    @patch("review_pull_request.tools.httpx.Client")
    def test_returns_error_on_rate_limit_403_message(self, MockClient):
        resp = _make_http_response(403, text="API rate limit exceeded for this resource")
        MockClient.return_value.get.return_value = resp

        reader = self._reader()
        result = reader.read_file("any.py", max_lines=8000)

        assert "rate limit" in result.lower()

    @patch("review_pull_request.tools.httpx.Client")
    def test_returns_error_for_too_large_file(self, MockClient):
        resp = _make_http_response(403, text="This file is too large to display")
        resp.json.return_value = {"message": "This file is too large to display. View it on GitHub"}
        MockClient.return_value.get.return_value = resp

        reader = self._reader()
        result = reader.read_file("big.py", max_lines=8000)

        assert "too large" in result.lower()
        assert "Error" in result

    @patch("review_pull_request.tools.httpx.Client")
    def test_returns_error_for_binary_content(self, MockClient):
        binary_bytes = b"\x00\xff\xfe\x81" * 100
        resp = _make_http_response(200, content=binary_bytes)
        resp.text = binary_bytes.decode("latin-1")
        MockClient.return_value.get.return_value = resp

        reader = self._reader()
        result = reader.read_file("image.png", max_lines=8000)

        assert "binary" in result.lower()
        assert "Error" in result

    @patch("review_pull_request.tools.httpx.Client")
    def test_truncates_long_files(self, MockClient):
        lines = [f"line {i}\n" for i in range(200)]
        content = "".join(lines)
        MockClient.return_value.get.return_value = _make_http_response(200, text=content)

        reader = self._reader()
        result = reader.read_file("long.py", max_lines=10)

        assert "TRUNCATED" in result
        assert "10 lines" in result
        # Only first 10 lines of content present
        assert "line 0" in result
        assert "line 11" not in result

    @patch("review_pull_request.tools.httpx.Client")
    def test_returns_error_on_network_failure(self, MockClient):
        import httpx as _httpx
        MockClient.return_value.get.side_effect = _httpx.TimeoutException("timeout")

        reader = self._reader()
        result = reader.read_file("any.py", max_lines=8000)

        assert "Error" in result
        assert "GitHub" in result

    @patch("review_pull_request.tools.httpx.Client")
    def test_passes_auth_header(self, MockClient):
        MockClient.return_value.get.return_value = _make_http_response(200, text="content")

        reader = GitHubFileReader("o", "r", "sha1", "mytoken")
        reader.read_file("file.py", max_lines=8000)

        # Headers are passed to the Client constructor, not per-call
        client_kwargs = MockClient.call_args[1]
        assert client_kwargs["headers"]["Authorization"] == "Bearer mytoken"
        assert "raw" in client_kwargs["headers"]["Accept"]

    @patch("review_pull_request.tools.httpx.Client")
    def test_close_calls_client_close(self, MockClient):
        reader = GitHubFileReader("o", "r", "sha", "tok")
        reader.close()
        MockClient.return_value.close.assert_called_once()

    @patch("review_pull_request.tools.httpx.Client")
    def test_del_calls_client_close(self, MockClient):
        reader = GitHubFileReader("o", "r", "sha", "tok")
        reader.__del__()
        MockClient.return_value.close.assert_called_once()

    @patch("review_pull_request.tools.httpx.Client")
    def test_context_manager_closes_client(self, MockClient):
        with GitHubFileReader("o", "r", "sha", "tok") as reader:
            pass
        MockClient.return_value.close.assert_called_once()

    @patch("review_pull_request.tools.httpx.Client")
    def test_context_manager_returns_reader(self, MockClient):
        with GitHubFileReader("o", "r", "sha", "tok") as reader:
            assert isinstance(reader, GitHubFileReader)

    @patch("review_pull_request.tools.httpx.Client")
    def test_cache_evicts_oldest_when_full(self, MockClient):
        MockClient.return_value.get.return_value = _make_http_response(200, text="x")
        reader = self._reader()
        # Fill cache to the limit
        for i in range(GitHubFileReader._MAX_CACHE):
            reader._cache[(f"file{i}.py", 8000)] = f"content{i}"
        # Seed first entry so we can check it's evicted
        first_key = next(iter(reader._cache))
        assert first_key in reader._cache

        reader.read_file("new_file.py", 8000)

        assert first_key not in reader._cache
        assert len(reader._cache) == GitHubFileReader._MAX_CACHE


class TestExecuteReadFileWithGitHubReader:
    def test_always_uses_reader_when_set(self, tmp_path):
        """When github_reader is provided, it is always used — even if file exists locally."""
        (tmp_path / "local.py").write_text("local content\n")

        reader = MagicMock(spec=GitHubFileReader)
        reader.read_file.return_value = "remote content\n"
        result = execute_read_file("local.py", str(tmp_path), 8000, reader)

        assert result == "remote content\n"
        reader.read_file.assert_called_once_with("local.py", 8000)

    def test_no_fallback_when_reader_is_none(self, tmp_path):
        """Without a reader, missing file returns the original error."""
        result = execute_read_file("missing.py", str(tmp_path), 8000, github_reader=None)
        assert "file not found" in result
        assert "Error" in result

    def test_traversal_blocked_even_with_reader(self, tmp_path):
        """Path-traversal guard fires before any fallback attempt."""
        reader = MagicMock(spec=GitHubFileReader)
        result = execute_read_file("../../etc/passwd", str(tmp_path), 8000, reader)

        assert "access denied" in result
        reader.read_file.assert_not_called()

    @patch("review_pull_request.tools.httpx.Client")
    def test_github_error_propagated_to_llm(self, MockClient, tmp_path):
        """GitHub API errors are returned as strings (not raised)."""
        MockClient.return_value.get.return_value = _make_http_response(500)

        reader = GitHubFileReader("owner", "repo", "sha", "tok")
        result = execute_read_file("src/foo.py", str(tmp_path), 8000, reader)

        assert "Error" in result
        assert "500" in result


class TestSiblingDirectoryTraversal:
    def test_sibling_with_same_prefix_is_rejected(self, tmp_path):
        repo = tmp_path / "proj"
        sibling = tmp_path / "proj-secrets"
        repo.mkdir()
        sibling.mkdir()
        (sibling / "key.txt").write_text("SECRET")

        result = execute_read_file("../proj-secrets/key.txt", str(repo), 100)

        assert "access denied" in result
        assert "SECRET" not in result


class TestReadErrorType:
    def test_errors_are_read_error_instances(self, tmp_path):
        from review_pull_request.tools import ReadError

        result = execute_read_file("missing.py", str(tmp_path), 100)

        assert isinstance(result, ReadError)
        assert result.startswith("Error:")

    def test_content_that_starts_with_error_is_plain_content(self, tmp_path):
        from review_pull_request.tools import ReadError

        (tmp_path / "log.txt").write_text("Error: something happened\n")

        result = execute_read_file("log.txt", str(tmp_path), 100)

        assert not isinstance(result, ReadError)
        assert result == "Error: something happened\n"


class TestExclusionsApplyToReads:
    def test_excluded_path_is_refused_and_never_fetched(self, tmp_path):
        from review_pull_request.tools import ReadError

        reader = MagicMock()
        result = execute_read_file(".env", str(tmp_path), 100, reader, exclusions=["**/.env"])

        assert isinstance(result, ReadError)
        assert "excluded" in result
        reader.read_file.assert_not_called()

    def test_path_tricks_do_not_bypass_the_exclusion(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / ".env").write_text("SECRET=1")

        for sneaky in ("src/../.env", "./.env", "/.env"):
            result = execute_read_file(sneaky, str(tmp_path), 100, None, exclusions=["**/.env"])
            assert "SECRET" not in result, sneaky

    def test_non_excluded_paths_still_read(self, tmp_path):
        (tmp_path / "a.py").write_text("print(1)\n")

        assert execute_read_file("a.py", str(tmp_path), 100, None, exclusions=["**/.env"]) == "print(1)\n"
