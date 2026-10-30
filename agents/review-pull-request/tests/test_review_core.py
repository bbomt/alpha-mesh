"""The review core at a chosen commit (PMAT-314): review_commit(o, r, B, X, desc) reviews exactly
the compare diff B...X with files read at X, and posts nothing. GitHub is faked at the boundary."""

from unittest.mock import patch

import httpx
import pytest

from review_pull_request.config import Config, SENSITIVE_PATTERNS
from review_pull_request.incremental import COMPARE_MAX_FILES, get_diff
from review_pull_request.review import main, review_commit
from tests import test_golden_master as gm
from tests.helpers import chat_model_factory

X = "5" * 40  # a commit chosen on purpose, not the PR head


@pytest.fixture
def _no_github_reader():
    """Overrides conftest's stub reader: the real GitHubFileReader runs against the fake."""
    yield


def _fakes(github, model):
    cfg = Config(model="m", api_url="http://x", api_token="t", exclusions=list(SENSITIVE_PATTERNS))
    return cfg, [
        patch("review_pull_request.session.make_chat_model", chat_model_factory(model)),
        patch("subprocess.run", github.run),
        patch.object(httpx.HTTPTransport, "handle_request", lambda self, req: github.handle(req)),
    ]


def test_the_core_reviews_the_compare_diff_base_to_x_with_files_read_at_x(monkeypatch):
    monkeypatch.setitem(gm.DIFFS, f"{gm.BASE}...{X}", gm.DIFFS[f"{gm.BASE}...{gm.HEAD}"])
    github, model, published = gm.FakeGitHub(), gm.Model(), []
    cfg, patches = _fakes(github, model)
    for p in patches:
        p.start()
    try:
        code = review_commit(gm.OWNER, gm.REPO, gm.BASE, X, "Eval description.", config=cfg,
                             token=gm.TOKEN, publish=published.append, all_excluded=lambda paths: 0)
    finally:
        patch.stopall()

    assert code == 0
    assert github.diff_ranges == [f"{gm.BASE}...{X}"]
    assert github.reads and all(ref == X for _, ref in github.reads)
    assert github.trees == [X]
    assert "Eval description." in str(model.seen[0])
    assert github.posted == [] and github.unexpected == []
    assert len(published) == 1 and f"`{X}`" in published[0]


def _diff_of(n_files):
    return "".join(gm._file_diff(f"src/f{i}.py", "x = 1") for i in range(n_files))


def _compare_response(text):
    return httpx.Response(200, text=text, request=httpx.Request("GET", "https://api.github.com"))


def test_get_diff_returns_a_diff_below_the_compare_file_limit():
    diff = _diff_of(COMPARE_MAX_FILES - 1)
    with patch("httpx.get", return_value=_compare_response(diff)):
        assert get_diff("o", "r", "b", "h", "t") == diff


def test_get_diff_refuses_a_diff_at_the_compare_file_limit():
    with patch("httpx.get", return_value=_compare_response(_diff_of(COMPARE_MAX_FILES))), \
            pytest.raises(RuntimeError, match="300-file compare limit"):
        get_diff("o", "r", "b", "h", "t")


def test_a_full_review_at_the_compare_file_limit_fails_loudly_and_posts_nothing(monkeypatch, capsys):
    monkeypatch.setitem(gm.DIFFS, f"{gm.BASE}...{gm.HEAD}", _diff_of(COMPARE_MAX_FILES))
    github, model = gm.FakeGitHub(), gm.Model()
    cfg, patches = _fakes(github, model)
    for p in patches + [patch("review_pull_request.review.load_config", return_value=cfg)]:
        p.start()
    try:
        code = main([gm.PR_URL])
    finally:
        patch.stopall()

    assert code == 1
    assert "ERROR: failed to get diff: the diff has 300 files" in capsys.readouterr().err
    assert model.seen == [] and github.posted == []
