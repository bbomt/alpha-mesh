"""I1 (spec PMAT-237 5.4): a review killed and resumed against the deployed DynamoDB table and S3 bucket.

Needs AWS credentials (SSO dev-EC2) and the deployed PmatAlphaMeshReviewCheckpointsStack-dev, so it
runs only with RUN_AWS_INTEGRATION=1:

    RUN_AWS_INTEGRATION=1 uv run pytest tests/test_integration_checkpoints.py

The model and GitHub are the fakes of tests/fixtures/run_review.py. The thread is deleted afterwards.
"""

import os
import pwd
import uuid

import boto3
import pytest

from review_pull_request.checkpoints import DEFAULT_REGION, make_checkpointer, thread_id
from review_pull_request.config import SENSITIVE_PATTERNS
from tests.test_resume import FILES, Review, assert_each_file_once, file_calls

pytestmark = pytest.mark.skipif(os.environ.get("RUN_AWS_INTEGRATION") != "1",
                                reason="needs AWS: set RUN_AWS_INTEGRATION=1")


def test_i1_kill_and_resume_against_the_deployed_store(tmp_path, monkeypatch):
    home = pwd.getpwuid(os.getuid()).pw_dir  # conftest points HOME at tmp_path; AWS config lives here
    monkeypatch.setenv("HOME", home)
    head = f"i1-{uuid.uuid4().hex}"  # a thread of its own
    thread = thread_id("owner", "repo", 42, head, "full", SENSITIVE_PATTERNS)
    r = Review(tmp_path)
    r.env.update(HOME=home, RR_STORE="aws", RR_HEAD=head)
    saver = make_checkpointer()
    config = {"configurable": {"thread_id": thread}}
    try:
        r.kill_while_hung("src/f3.py")
        saved = saver.get_tuple(config).checkpoint["channel_values"]["results"]
        assert sorted(saved) == ["src/f1.py", "src/f2.py"]
        before = len(r.calls())

        resumed = r.run()

        assert resumed.returncode == 0, resumed.stderr
        assert "Resuming: 2 file(s) from checkpoint, 2 to review" in resumed.stderr
        assert file_calls(r.calls()[before:]) == FILES[2:]
        assert_each_file_once(r.posts()[0])
    finally:
        saver.delete_thread(thread)
    assert saver.get_tuple(config) is None

    # The saver's own S3 lifecycle rule expires objects after checkpointTtlDays (spec 3.10).
    region = os.environ.get("AWS_REGION") or DEFAULT_REGION
    prefix = f"/pmat/{os.environ.get('DEPLOY_ENV', 'dev')}/alpha-mesh/review-pull-request"
    ssm = boto3.client("ssm", region_name=region)
    ttl_days = int(ssm.get_parameter(Name=f"{prefix}/checkpoint-ttl-days")["Parameter"]["Value"])
    bucket = ssm.get_parameter(Name=f"{prefix}/checkpoint-bucket")["Parameter"]["Value"]
    rules = boto3.client("s3", region_name=region).get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    assert any(rule["Filter"].get("Tag") == {"Key": "ttl-days", "Value": str(ttl_days)}
               and rule["Expiration"]["Days"] == ttl_days for rule in rules), rules
