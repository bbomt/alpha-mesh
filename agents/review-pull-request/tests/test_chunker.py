"""Tests for chunker.py — build_chunks."""
import pytest

from review_pull_request.chunker import build_chunks, chunk_exists, FileChunk


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


class TestChunkExists:
    def test_no_match(self):
        assert not chunk_exists("src/main.py", ["package-lock.json", "*/dist/*"])[0]

    def test_fnmatch_dist(self):
        assert chunk_exists("dist/bundle.js", ["*/dist/*"])[0]

    def test_fnmatch_node_modules(self):
        assert chunk_exists("node_modules/foo/bar.js", ["*/node_modules/*"])[0]

    def test_exact_lockfile(self):
        assert chunk_exists("package-lock.json", ["package-lock.json"])[0]


# --- chunks with no code to review (PMAT-229) ---

DELETED = (
    "diff --git a/old.py b/old.py\n"
    "deleted file mode 100644\n"
    "index 1234567..0000000\n"
    "--- a/old.py\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n"
    "-a\n"
    "-b\n"
)
PURE_RENAME = (
    "diff --git a/src/a.py b/lib/a.py\n"
    "similarity index 100%\n"
    "rename from src/a.py\n"
    "rename to lib/a.py\n"
)
EDITED_RENAME = (
    "diff --git a/src/b.py b/lib/b.py\n"
    "similarity index 90%\n"
    "rename from src/b.py\n"
    "rename to lib/b.py\n"
    "--- a/src/b.py\n"
    "+++ b/lib/b.py\n"
    "@@ -1 +1 @@\n"
    "-x\n"
    "+y\n"
)
NEW_FILE = (
    "diff --git a/new.py b/new.py\n"
    "new file mode 100644\n"
    "index 0000000..1234567\n"
    "--- /dev/null\n"
    "+++ b/new.py\n"
    "@@ -0,0 +1 @@\n"
    "+z\n"
)


def test_deleted_files_are_not_chunked():
    assert build_chunks(DELETED, []) == []


def test_pure_renames_are_not_chunked():
    assert build_chunks(PURE_RENAME, []) == []


def test_renames_with_edits_and_new_files_are_kept():
    chunks = build_chunks(EDITED_RENAME + NEW_FILE, [])

    assert [c.file_path for c in chunks] == ["lib/b.py", "new.py"]


def test_skipped_chunks_are_reported_with_a_reason():
    skipped = []

    chunks = build_chunks(DELETED + PURE_RENAME + NEW_FILE, [], skipped=skipped)

    assert [c.file_path for c in chunks] == ["new.py"]
    assert skipped == [("old.py", "deleted"), ("lib/a.py", "renamed without changes")]
