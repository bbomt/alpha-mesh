"""Archive the eval runs that committed files depend on, in S3 (PMAT-336).

    uv run python eval/scripts/archive_runs.py push ~/.mesh/eval-runs/<run>      # upload eval-runs/<run>.tar.gz
    uv run python eval/scripts/archive_runs.py pull <run> [--to <dir>]           # default ~/.mesh/eval-runs/<run>
    uv run python eval/scripts/archive_runs.py prune [--yes]                     # dry run without --yes

A run is kept while a committed file names it: eval/baseline.json's `run` and eval/judge/sample.json's `run` (the run
whose reviews the calibration labels score). `prune` deletes every other run, and refuses while a named run is not in
the bucket. The bucket is versioned: a deleted run can be restored for 30 days (infra/cdk, EvalRunsStack).

An archive holds results.json, run.json and traces/ only (no checkpoints, locks or promptfoo state). Trace paths are
written relative to the run folder, and promptfoo's basePath and outputPath are dropped, so no path names this machine
and a pulled run works from any folder (agent_provider._stored_review resolves a relative trace).

The bucket is EVAL_RUNS_BUCKET, else SSM /pmat/<DEPLOY_ENV>/alpha-mesh/review-pull-request/eval-runs-bucket
(DEPLOY_ENV default dev). Needs AWS credentials (SSO dev-EC2).
"""

import argparse
import io
import json
import os
import sys
import tarfile
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parent.parent / "promptfoo")]
import case_rules  # noqa: E402

from review_pull_request.checkpoints import DEFAULT_REGION  # noqa: E402

PREFIX = "eval-runs/"
SUFFIX = ".tar.gz"
BUCKET_ENV = "EVAL_RUNS_BUCKET"
LOCAL_RUNS = Path.home() / ".mesh" / "eval-runs"
DEPENDENTS = (case_rules.EVAL / "baseline.json", case_rules.EVAL / "judge" / "sample.json")


class ArchiveError(Exception):
    """A run can't be archived, fetched or pruned safely."""


def needed_runs() -> set[str]:
    """The runs the committed files name."""
    names = set()
    for path in DEPENDENTS:
        run = json.loads(path.read_text()).get("run")
        if not run:
            raise ArchiveError(f"{path.name} names no run, so prune can't tell what it depends on")
        names.add(run)
    return names


def _portable(value, run_dir: str):
    """value with every path inside the run folder made relative to it."""
    if isinstance(value, dict):
        return {k: _portable(v, run_dir) for k, v in value.items()}
    if isinstance(value, list):
        return [_portable(v, run_dir) for v in value]
    if isinstance(value, str) and value.startswith(run_dir + "/"):
        return value[len(run_dir) + 1:]
    return value


def _add(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)  # no owner, mode 644, mtime 0: nothing about this machine
    info.size, info.mode = len(data), 0o644
    tar.addfile(info, io.BytesIO(data))


def pack(run_dir: Path) -> bytes:
    """The run's archive: results.json (made portable), run.json and traces/."""
    real = run_dir.resolve()
    if not (real / "results.json").is_file() or not (real / "run.json").is_file():
        raise ArchiveError(f"{run_dir} has no results.json and run.json: not an eval run")
    results = _portable(json.loads((real / "results.json").read_text()), str(real))
    for key in ("basePath", "outputPath"):  # promptfoo's own record of where it ran; nothing reads it
        results.get("config", {}).pop(key, None)
    files = {"results.json": json.dumps(results).encode(), "run.json": (real / "run.json").read_bytes()}
    if (real / "traces").is_dir():
        for path in sorted((real / "traces").rglob("*")):
            if path.is_file():
                files[str(path.relative_to(real))] = path.read_bytes()
    for name, data in files.items():
        text = data.decode(errors="replace")
        for marker in (str(real), str(Path.home())):
            if marker in text:
                raise ArchiveError(f"{name} still names this machine ({marker}): not pushed")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            _add(tar, name, data)
    return buf.getvalue()


def push(run_dir: Path, s3, bucket: str) -> str:
    key = f"{PREFIX}{run_dir.resolve().name}{SUFFIX}"
    archive = pack(run_dir)
    s3.put_object(Bucket=bucket, Key=key, Body=archive)
    print(f"Pushed s3://{bucket}/{key} ({len(archive) / 1e6:.1f} MB)")
    return key


def _allowed(member: tarfile.TarInfo) -> bool:
    parts = Path(member.name).parts
    return (member.isfile() or member.isdir()) and not member.name.startswith("/") and ".." not in parts and (
        member.name in ("results.json", "run.json") or (parts and parts[0] == "traces"))


def pull(name: str, s3, bucket: str, target: Path | None = None) -> Path:
    target = Path(target or LOCAL_RUNS / name).expanduser()
    if target.exists() and any(target.iterdir()):
        raise ArchiveError(f"{target} is not empty: pull into a new folder")
    key = f"{PREFIX}{name}{SUFFIX}"
    data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        bad = [m.name for m in tar.getmembers() if not _allowed(m)]
        if bad:
            raise ArchiveError(f"{key} holds what a run archive never does: {', '.join(bad)}")
        target.mkdir(parents=True, exist_ok=True)
        tar.extractall(target, filter="data")
    print(f"Pulled s3://{bucket}/{key} to {target}")
    return target


def archived_runs(s3, bucket: str) -> set[str]:
    names = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=PREFIX):
        for obj in page.get("Contents", []):
            rest = obj["Key"][len(PREFIX):]
            if rest.endswith(SUFFIX) and "/" not in rest:
                names.add(rest[:-len(SUFFIX)])
    return names


def prune(s3, bucket: str, yes: bool) -> list[str]:
    """Delete every archived run no committed file names (a dry run unless yes)."""
    needed, present = needed_runs(), archived_runs(s3, bucket)
    missing = sorted(needed - present)
    if missing:
        raise ArchiveError(f"needed but not in s3://{bucket}/{PREFIX}: {', '.join(missing)}. Push them first; "
                           "nothing was deleted")
    stale = sorted(present - needed)
    for name in sorted(needed):
        print(f"keep    {name}")
    for name in stale:
        if yes:
            s3.delete_object(Bucket=bucket, Key=f"{PREFIX}{name}{SUFFIX}")
        print(f"{'deleted' if yes else 'would delete'} {name}")
    if stale and not yes:
        print("Dry run: nothing deleted. Rerun with --yes.")
    return stale


def _region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or DEFAULT_REGION


def client():
    return boto3.client("s3", region_name=_region())


def bucket_name() -> str:
    if os.environ.get(BUCKET_ENV):
        return os.environ[BUCKET_ENV]
    name = f"/pmat/{os.environ.get('DEPLOY_ENV', 'dev')}/alpha-mesh/review-pull-request/eval-runs-bucket"
    return boto3.client("ssm", region_name=_region()).get_parameter(Name=name)["Parameter"]["Value"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Archive the eval runs that committed files depend on, in S3.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("push", help="upload a run").add_argument("run_dir", type=Path)
    p = sub.add_parser("pull", help="fetch a run back")
    p.add_argument("name")
    p.add_argument("--to", type=Path, help=f"default {LOCAL_RUNS}/<name>")
    sub.add_parser("prune", help="delete runs no committed file names").add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    try:
        s3, bucket = client(), bucket_name()
        if args.command == "push":
            push(args.run_dir, s3, bucket)
        elif args.command == "pull":
            pull(args.name, s3, bucket, args.to)
        else:
            prune(s3, bucket, args.yes)
    except ArchiveError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except (BotoCoreError, ClientError) as e:
        print(f"ERROR: S3 or SSM ({_region()}): {e}\n  Needs AWS credentials (SSO dev-EC2): check "
              "`aws sts get-caller-identity` (`aws sso login` if the session expired).", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
