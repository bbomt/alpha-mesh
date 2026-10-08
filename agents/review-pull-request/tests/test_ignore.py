"""Tests for the global .pr-review-ignore file (PMAT-229)."""

from pathlib import Path

import pytest
from types import SimpleNamespace

from review_pull_request import config as config_module
from review_pull_request.chunker import build_chunks
from review_pull_request.config import IGNORE_FILE, load_config, load_ignore_patterns


def _diff(*paths):
    return "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n+x\n" for p in paths
    )


def test_ignore_file_default_is_alpha_mesh_root():
    # Derived from this test file's location, independently of config.py's own computation.
    repo_root = Path(__file__).resolve().parents[3]
    assert (repo_root / "bin" / "mesh").exists()  # sanity: this really is the alpha-mesh root
    assert IGNORE_FILE == repo_root / ".pr-review-ignore"


def test_load_ignore_patterns_skips_comments_and_blank_lines(tmp_path):
    f = tmp_path / ".pr-review-ignore"
    f.write_text("# docs\nCLAUDE.md\n\n  .mesh/*  \n**/README.md\n")

    assert load_ignore_patterns(f) == ["CLAUDE.md", ".mesh/*", "**/README.md"]


def test_load_ignore_patterns_missing_file_returns_empty(tmp_path):
    assert load_ignore_patterns(tmp_path / "nope") == []


def test_chunker_applies_the_documented_patterns():
    patterns = ["CLAUDE.md", ".mesh/*", "**/README.md"]
    diff = _diff(
        "CLAUDE.md",
        ".mesh/MEMORY.md",
        "README.md",
        "agents/dream/README.md",
        "src/a.py",
    )

    kept = [c.file_path for c in build_chunks(diff, patterns)]

    assert kept == ["src/a.py"]


def test_load_config_merges_ignore_file_with_toml_exclusions(tmp_path, monkeypatch):
    ignore = tmp_path / ".pr-review-ignore"
    ignore.write_text("CLAUDE.md\n")
    toml_file = tmp_path / "review_config.toml"
    toml_file.write_text('[exclusions]\npatterns = ["*.lock"]\n')
    monkeypatch.setattr(config_module, "IGNORE_FILE", ignore)
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)  # never read a real .env
    for var, val in [("CODE_REVIEW_URL", "http://x"), ("CODE_REVIEW_TOKEN", "t"), ("CODE_REVIEW_MODEL", "m")]:
        monkeypatch.setenv(var, val)
    args = SimpleNamespace(config=str(toml_file), model=None, base=None, repo_path=None,
                           dry_run=False, verbose=False, hide_pr_comment=False)

    cfg = load_config(args)

    assert "*.lock" in cfg.exclusions
    assert "CLAUDE.md" in cfg.exclusions


def test_load_ignore_patterns_tolerates_a_utf8_bom(tmp_path):
    f = tmp_path / ".pr-review-ignore"
    f.write_bytes(b"\xef\xbb\xbfCLAUDE.md\n.mesh/*\n")

    assert load_ignore_patterns(f) == ["CLAUDE.md", ".mesh/*"]


def test_unreadable_ignore_file_aborts_instead_of_reviewing_more_files(tmp_path, capsys):
    f = tmp_path / ".pr-review-ignore"
    f.write_bytes(b"\xff\xfe\x00bad")

    with pytest.raises(SystemExit) as exc:
        load_ignore_patterns(f)

    assert exc.value.code != 0
    assert "pr-review-ignore" in capsys.readouterr().err


def test_shipped_ignore_file_excludes_nested_claude_md_and_readmes():
    patterns = load_ignore_patterns(IGNORE_FILE)
    diff = _diff("CLAUDE.md", "packages/foo/CLAUDE.md", "docs/README.md", "src/a.py")

    assert [c.file_path for c in build_chunks(diff, patterns)] == ["src/a.py"]


def test_shipped_ignore_file_keeps_secret_bearing_paths_away_from_the_llm():
    patterns = load_ignore_patterns(IGNORE_FILE)
    secret_paths = [
        ".claude/settings.json", "sub/.claude/settings.json", ".claude/settings.local.json",
        ".mesh/MEMORY.md", ".env", "svc/.env", "config/active.json",
    ]
    diff = _diff(*secret_paths, ".env.example", "src/a.py")

    assert [c.file_path for c in build_chunks(diff, patterns)] == [".env.example", "src/a.py"]


def test_load_config_warns_when_the_ignore_file_is_not_in_an_alpha_mesh_checkout(tmp_path, monkeypatch, capsys):
    toml_file = tmp_path / "review_config.toml"
    toml_file.write_text("")
    monkeypatch.setattr(config_module, "IGNORE_FILE", tmp_path / ".pr-review-ignore")  # no bin/mesh next to it
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)
    for var, val in [("CODE_REVIEW_URL", "http://x"), ("CODE_REVIEW_TOKEN", "t"), ("CODE_REVIEW_MODEL", "m")]:
        monkeypatch.setenv(var, val)
    args = SimpleNamespace(config=str(toml_file), model=None, base=None, repo_path=None,
                           dry_run=False, verbose=False, hide_pr_comment=False)

    load_config(args)

    assert "global exclusions" in capsys.readouterr().err.lower()


def test_token_bearing_paths_are_excluded_even_if_the_ignore_file_is_missing(tmp_path, monkeypatch):
    toml_file = tmp_path / "review_config.toml"
    toml_file.write_text("")
    monkeypatch.setattr(config_module, "IGNORE_FILE", tmp_path / "nowhere" / ".pr-review-ignore")
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)
    for var, val in [("CODE_REVIEW_URL", "http://x"), ("CODE_REVIEW_TOKEN", "t"), ("CODE_REVIEW_MODEL", "m")]:
        monkeypatch.setenv(var, val)
    args = SimpleNamespace(config=str(toml_file), model=None, base=None, repo_path=None,
                           dry_run=False, verbose=False, hide_pr_comment=False)

    cfg = load_config(args)
    kept = [c.file_path for c in build_chunks(_diff(".env", ".claude/settings.json", ".mesh/MEMORY.md",
                                                     "config/active.json", "src/a.py"), cfg.exclusions)]

    assert kept == ["src/a.py"]


def test_patterns_are_normalised_so_gitignore_style_lines_actually_match(tmp_path):
    f = tmp_path / ".pr-review-ignore"
    f.write_text("build/\\n.env   # secrets\\n/vendor/*\\n".replace("\\n", "\n"))

    assert load_ignore_patterns(f) == ["**/build/*", ".env", "vendor/*"]
