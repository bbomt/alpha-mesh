"""Durable checkpoints for resumable reviews (spec PMAT-237 3.4, 3.6a, 3.10).

The only module that builds a checkpoint saver for production runs:
  * thread_id(...)        : the key a review resumes on (PR, head SHA, diff mode, exclusion policy; not the model)
  * lock_thread(thread)   : one run per thread on this machine (fcntl.flock, released when the process ends)
  * make_checkpointer()   : the standard DynamoDBSaver, table/bucket/TTL from SSM (env overrides);
                            an InMemorySaver for --dry-run, which needs no AWS
"""

import fcntl
import hashlib
import os
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from langgraph.checkpoint.memory import InMemorySaver
from langgraph_checkpoint_aws import DynamoDBSaver

DEFAULT_REGION = "eu-west-1"
TABLE_ENV = "ALPHA_MESH_REVIEW_PULL_REQ_CHECKPOINT_TABLE"
BUCKET_ENV = "ALPHA_MESH_REVIEW_PULL_REQ_CHECKPOINT_BUCKET"


class CheckpointStoreError(Exception):
    """The checkpoint store cannot be used (no AWS credentials, missing SSM parameter...)."""


class ThreadBusy(Exception):
    """Another process holds the run lock of this thread."""

    def __init__(self, pid: str) -> None:
        super().__init__(f"another review is running on this thread (pid {pid})")
        self.pid = pid


def policy_hash(patterns) -> str:
    """First 12 hex chars of sha256 over the sorted, de-duplicated exclusion patterns."""
    return hashlib.sha256("\n".join(sorted(set(patterns))).encode()).hexdigest()[:12]


def thread_id(owner: str, repo: str, pr_number, head_sha: str, mode: str, patterns) -> str:
    """mode is "full" or "since-{base_sha}". The model is deliberately not part of the key."""
    return f"review-pull-request:{owner}/{repo}#{pr_number}:{head_sha}:{mode}:{policy_hash(patterns)}"


def lock_thread(thread: str):
    """Take the run lock of a thread; returns the open lock file, which holds the lock until closed.

    The OS releases the lock when the process ends in any way (SIGKILL included), so a killed run
    can be resumed at once. Same machine only.
    """
    locks = Path.home() / ".mesh" / "locks"
    locks.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(thread.encode()).hexdigest()[:16]
    f = open(locks / f"review-pull-request-{name}.lock", "a+")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.seek(0)
        pid = f.read().strip() or "unknown"
        f.close()
        raise ThreadBusy(pid)
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


def _ssm_client(region: str):
    return boto3.client("ssm", region_name=region)


def make_checkpointer(dry_run: bool = False):
    """The DynamoDB checkpointer (S3 offload, compression, TTL) for DEPLOY_ENV (default dev)."""
    if dry_run:
        return InMemorySaver()
    env = os.environ.get("DEPLOY_ENV", "dev")
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION
    prefix = f"/pmat/{env}/alpha-mesh/review-pull-request"
    ssm = _ssm_client(region)

    def param(name: str) -> str:
        return ssm.get_parameter(Name=f"{prefix}/{name}")["Parameter"]["Value"]

    try:
        table = os.environ.get(TABLE_ENV) or param("checkpoint-table")
        bucket = os.environ.get(BUCKET_ENV) or param("checkpoint-bucket")
        ttl_days = int(param("checkpoint-ttl-days"))
    except (BotoCoreError, ClientError, ValueError) as e:
        raise CheckpointStoreError(
            f"cannot reach the checkpoint store ({prefix}/* in SSM, {region}): {e}\n"
            "  Reviews save their progress in DynamoDB/S3 and need AWS credentials (SSO dev-EC2).\n"
            "  Check `aws sts get-caller-identity` (`aws sso login` if the session expired),\n"
            "  or use --dry-run, which needs no AWS."
        ) from e
    return DynamoDBSaver(
        table_name=table,
        region_name=region,
        ttl_seconds=ttl_days * 86400,
        enable_checkpoint_compression=True,
        s3_offload_config={"bucket_name": bucket},
    )
