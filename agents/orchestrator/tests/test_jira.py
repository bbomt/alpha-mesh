"""Which comment counts as the owner's newest, from Jira's newest-first comment list."""

from orchestrator.jira import newest_owner_comment

WORKER = "MESSAGE-FROM-WORKER\n\nDone: ..."


def test_a_worker_comment_right_after_the_owner_s_does_not_hide_it():
    # PMAT-221, 2026-10-23: the owner's "merged" (13227) came 14 s before the worker's report (13228). Taking
    # only the newest comment hid the owner's, so it was never passed on.
    newest_first = [{"id": "13228", "body": WORKER},
                    {"id": "13227", "body": "✓ Squashed and merged pull request bbomt/PMATFE-website#14"},
                    {"id": "13226", "body": "* Create a fresh ticket for the cookie manager"}]
    assert newest_owner_comment(newest_first) == "13227"


def test_only_worker_comments_or_none_means_no_owner_comment():
    assert newest_owner_comment([{"id": "9", "body": WORKER}]) is None
    assert newest_owner_comment([]) is None


def test_the_marker_counts_only_as_the_first_line():
    quoted = [{"id": "5", "body": "Re the worker's note:\n> MESSAGE-FROM-WORKER\nlooks good"}]
    assert newest_owner_comment(quoted) == "5"


def test_logged_in_only_once_tokens_are_saved(tmp_path, monkeypatch):
    # Until then the check has no deadline (PMAT-306): the owner is pasting the login redirect URL.
    from orchestrator import jira
    monkeypatch.setattr(jira, "STORE", tmp_path / "atlassian-oauth.json")
    assert not jira.logged_in()
    jira.STORE.write_text('{"client": {}}')
    assert not jira.logged_in()
    jira.STORE.write_text('{"client": {}, "tokens": {}}')
    assert jira.logged_in()
