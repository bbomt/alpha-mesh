"""PathPolicy: the single place where repo paths are compared with exclusion patterns."""

import pytest

from review_pull_request.policy import PathPolicy


@pytest.mark.parametrize("path", [".env", ".ENV", "./.env", "/.env", "svc/../.env", "a/b/.env", "A/B/.Env"])
def test_env_in_any_spelling_is_excluded(path):
    assert PathPolicy(["**/.env"]).is_excluded(path)


@pytest.mark.parametrize("path", ["src/a.py", "envfile", "docs/env.md", "src/.environment/x"])
def test_ordinary_paths_are_not_excluded(path):
    assert not PathPolicy(["**/.env"]).is_excluded(path)


def test_directory_patterns_match_contents_at_any_depth():
    policy = PathPolicy(["**/.claude/*"])

    assert policy.is_excluded(".claude/settings.json")
    assert policy.is_excluded("sub/.Claude/settings.local.json")
    assert not policy.is_excluded("claude/settings.json")


def test_policies_combine_with_or():
    combined = PathPolicy(["**/.env"]) | PathPolicy(["**/README.md"])

    assert combined.is_excluded(".env") and combined.is_excluded("docs/README.md")
    assert not combined.is_excluded("src/a.py")


def test_empty_policy_excludes_nothing():
    assert not PathPolicy([]).is_excluded(".env")


@pytest.mark.parametrize("path", ["../.env", "../../.env", "svc/../../.env", ".."])
def test_paths_that_escape_the_root_are_excluded(path):
    assert PathPolicy(["**/.env"]).is_excluded(path)
    assert PathPolicy([]).is_excluded(path)  # fail closed even with no patterns
