"""Tests for chunker.py — build_chunks."""
import pytest

from review_pull_request.chunker import build_chunks, FileChunk
from review_pull_request.policy import PathPolicy


class TestBuildChunks:
    def test_single_file_diff(self):
        diff = """diff --git a/src/auth.py b/src/auth.py
index 1234567..abcdefg 100644
--- a/src/auth.py
+++ b/src/auth.py
@@ -1,3 +1,4 @@
+new line
"""
        chunks = build_chunks(diff, [])
        assert len(chunks) == 1
        assert chunks[0].file_path == "src/auth.py"
        assert "new line" in chunks[0].diff_text

    def test_multi_file_diff(self):
        diff = """diff --git a/src/main.py b/src/main.py
index 1111111..2222222 100644
--- a/src/main.py
+++ b/src/main.py
@@ -1,2 +1,3 @@
+line a

diff --git a/src/utils.py b/src/utils.py
index 3333333..4444444 100644
--- a/src/utils.py
+++ b/src/utils.py
@@ -1,2 +1,3 @@
+line b
"""
        chunks = build_chunks(diff, [])
        assert len(chunks) == 2
        assert chunks[0].file_path == "src/main.py"
        assert chunks[1].file_path == "src/utils.py"

    def test_excludes_lockfile(self):
        diff = """diff --git a/package-lock.json b/package-lock.json
index 111..222 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,3 +1,4 @@
+new dep
"""
        chunks = build_chunks(diff, ["package-lock.json"])
        assert len(chunks) == 0

    def test_excludes_dist(self):
        diff = """diff --git a/dist/bundle.js b/dist/bundle.js
index 111..222 100644
--- a/dist/bundle.js
+++ b/dist/bundle.js
@@ -1,3 +1,4 @@"""

        chunks = build_chunks(diff, ["*/dist/*"])
        assert len(chunks) == 0

    def test_excludes_build(self):
        diff = """diff --git a/build/output.js b/build/output.js
index 111..222 100644
--- a/build/output.js
+++ b/build/output.js"""

        chunks = build_chunks(diff, ["*/build/*"])
        assert len(chunks) == 0

    def test_excludes_wildcard_lock(self):
        diff = """diff --git a/yarn.lock b/yarn.lock
+new
diff --git a/pnpm-lock.yaml b/pnpm-lock.yaml
+new
"""
        chunks = build_chunks(diff, ["*.lock", "yarn.lock", "pnpm-lock.yaml"])
        assert len(chunks) == 0

    def test_empty_diff(self):
        assert build_chunks("", []) == []
        assert build_chunks("   \n\n", []) == []

    def test_diff_only_excluded_files(self):
        diff = """diff --git a/package-lock.json b/package-lock.json
+new
diff --git a/node_modules/foo/bar.js b/node_modules/foo/bar.js
+new
"""
        chunks = build_chunks(diff, ["package-lock.json", "*/node_modules/*"])
        assert len(chunks) == 0

    def test_preserves_order(self):
        diff = """diff --git a/file-c.py b/file-c.py
+change
diff --git a/file-a.py b/file-a.py
+change
diff --git a/file-b.py b/file-b.py
+change
"""
        chunks = build_chunks(diff, [])
        assert [c.file_path for c in chunks] == ["file-c.py", "file-a.py", "file-b.py"]

    def test_pick_b_path(self):
        diff = """diff --git a/old/path.py b/new/path.py
+change
"""
        chunks = build_chunks(diff, [])
        assert len(chunks) == 1
        assert chunks[0].file_path == "new/path.py"


class TestExclusionPatterns:
    def test_no_match(self):
        assert not PathPolicy(["package-lock.json", "*/dist/*"]).is_excluded("src/main.py")

    def test_fnmatch_dist(self):
        assert PathPolicy(["*/dist/*"]).is_excluded("dist/bundle.js")

    def test_fnmatch_node_modules(self):
        assert PathPolicy(["*/node_modules/*"]).is_excluded("node_modules/foo/bar.js")

    def test_exact_lockfile(self):
        assert PathPolicy(["package-lock.json"]).is_excluded("package-lock.json")


def test_exclusion_matching_ignores_case():
    diff = "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n+x\n"
        for p in (".ENV", ".Env.Local", "svc/.ENV", ".Claude/settings.json", "Config/Active.json", "src/a.py")
    )
    patterns = ["**/.env", "**/.env.*", "**/.claude/*", "config/active.json"]

    assert [c.file_path for c in build_chunks(diff, patterns)] == ["src/a.py"]
