"""Unit tests for checkpoints.py: thread key, policy hash, run lock, checkpointer settings (spec 3.4, 3.6a, 3.10)."""

import os

import pytest

from review_pull_request.checkpoints import (
    CheckpointStoreError,
    ThreadBusy,
    lock_thread,
    make_checkpointer,
    policy_hash,
    thread_id,
)


def test_policy_hash_ignores_pattern_order_and_duplicates():
    assert policy_hash(["*.env", "secrets/*"]) == policy_hash(["secrets/*", "*.env", "*.env"])


def test_policy_hash_changes_when_any_pattern_changes():
    assert policy_hash(["*.env"]) != policy_hash(["*.env", "x"])
    assert policy_hash(["*.env"]) != policy_hash(["*.envx"])


def test_policy_hash_is_12_hex_chars():
    h = policy_hash([])
    assert len(h) == 12 and int(h, 16) >= 0


def test_thread_id_names_pr_sha_mode_and_policy_but_not_the_model():
    tid = thread_id("o", "r", 7, "abc", "full", ["*.env"])
    assert tid == f"review-pull-request:o/r#7:abc:full:{policy_hash(['*.env'])}"


def test_thread_id_for_an_incremental_review_names_the_base():
    assert ":since-base1:" in thread_id("o", "r", 7, "abc", "since-base1", [])


def test_second_lock_on_a_thread_names_the_holders_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    held = lock_thread("t1")
    try:
        with pytest.raises(ThreadBusy) as e:
            lock_thread("t1")
        assert e.value.pid == str(os.getpid())
        lock_thread("t2").close()  # another thread is not blocked
    finally:
        held.close()
    lock_thread("t1").close()  # released once the holder closes


def test_lock_files_live_under_mesh_locks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    lock_thread("t1").close()
    [lock] = (tmp_path / ".mesh" / "locks").iterdir()
    assert lock.name.startswith("review-pull-request-") and lock.suffix == ".lock"


class _FakeSSM:
    def __init__(self, params):
        self.params = params
        self.asked = []

    def get_parameter(self, Name):
        self.asked.append(Name)
        return {"Parameter": {"Value": self.params[Name]}}


PREFIX = "/pmat/dev/alpha-mesh/review-pull-request"


def _saver_args(monkeypatch, ssm):
    captured = {}
    monkeypatch.setattr("review_pull_request.checkpoints._ssm_client", lambda region: ssm)
    monkeypatch.setattr("review_pull_request.checkpoints.DynamoDBSaver", lambda **kw: captured.update(kw) or kw)
    return captured


def test_checkpointer_reads_table_bucket_and_ttl_from_ssm(monkeypatch):
    for var in ("ALPHA_MESH_REVIEW_PULL_REQ_CHECKPOINT_TABLE", "ALPHA_MESH_REVIEW_PULL_REQ_CHECKPOINT_BUCKET",
                "DEPLOY_ENV"):
        monkeypatch.delenv(var, raising=False)
    ssm = _FakeSSM({f"{PREFIX}/checkpoint-table": "tbl", f"{PREFIX}/checkpoint-bucket": "bkt",
                    f"{PREFIX}/checkpoint-ttl-days": "2"})
    kw = _saver_args(monkeypatch, ssm)
    make_checkpointer()
    assert kw["table_name"] == "tbl"
    assert kw["s3_offload_config"]["bucket_name"] == "bkt"
    assert kw["ttl_seconds"] == 2 * 86400
    assert kw["enable_checkpoint_compression"] is True


def test_env_overrides_table_and_bucket(monkeypatch):
    monkeypatch.setenv("ALPHA_MESH_REVIEW_PULL_REQ_CHECKPOINT_TABLE", "t-env")
    monkeypatch.setenv("ALPHA_MESH_REVIEW_PULL_REQ_CHECKPOINT_BUCKET", "b-env")
    monkeypatch.setenv("DEPLOY_ENV", "staging")
    ssm = _FakeSSM({"/pmat/staging/alpha-mesh/review-pull-request/checkpoint-ttl-days": "3"})
    kw = _saver_args(monkeypatch, ssm)
    make_checkpointer()
    assert (kw["table_name"], kw["s3_offload_config"]["bucket_name"]) == ("t-env", "b-env")
    assert kw["ttl_seconds"] == 3 * 86400
    assert ssm.asked == ["/pmat/staging/alpha-mesh/review-pull-request/checkpoint-ttl-days"]


def test_no_aws_credentials_is_a_clear_error(monkeypatch):
    from botocore.exceptions import NoCredentialsError

    class _NoCreds:
        def get_parameter(self, Name):
            raise NoCredentialsError()

    _saver_args(monkeypatch, _NoCreds())
    with pytest.raises(CheckpointStoreError) as e:
        make_checkpointer()
    assert "AWS" in str(e.value) and "--dry-run" in str(e.value)


def test_dry_run_needs_no_aws(monkeypatch):
    monkeypatch.setattr("review_pull_request.checkpoints._ssm_client", lambda region: 1 / 0)
    from langgraph.checkpoint.memory import InMemorySaver

    assert isinstance(make_checkpointer(dry_run=True), InMemorySaver)


def test_a_saver_that_cannot_be_built_is_the_same_clear_error(monkeypatch):
    from botocore.exceptions import ClientError

    ssm = _FakeSSM({f"{PREFIX}/checkpoint-table": "tbl", f"{PREFIX}/checkpoint-bucket": "bkt",
                    f"{PREFIX}/checkpoint-ttl-days": "2"})
    monkeypatch.setattr("review_pull_request.checkpoints._ssm_client", lambda region: ssm)

    def broken_saver(**kw):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "PutBucketLifecycleConfiguration")

    monkeypatch.setattr("review_pull_request.checkpoints.DynamoDBSaver", broken_saver)
    with pytest.raises(CheckpointStoreError) as e:
        make_checkpointer()
    assert "AccessDenied" in str(e.value) and "--dry-run" in str(e.value)
