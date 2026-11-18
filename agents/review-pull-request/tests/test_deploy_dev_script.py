"""scripts/deploy-dev-from-ec2.sh finds the region and expected account at run time, as none is committed
(PMAT-347). The real script runs in a copy of its folder layout, with a stub `aws` and a stub deploy step."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# AWS's documented example account, and another built at run time: no other 12-digit literal is committed.
EXAMPLE, OTHER = "123456789012", "4" * 12
SCRIPT = Path(__file__).resolve().parents[1] / "scripts/deploy-dev-from-ec2.sh"


@pytest.fixture
def run(tmp_path):
    root = tmp_path / "alpha-mesh"
    scripts, infra, bin_dir = (root / "agents/review-pull-request/scripts",
                               root / "agents/review-pull-request/infra/scripts", tmp_path / "bin")
    for d in (scripts, infra, bin_dir):
        d.mkdir(parents=True)
    shutil.copy(SCRIPT, scripts / SCRIPT.name)
    (infra / "deploy-pipeline-infra.sh").write_text('#!/bin/bash\necho "DEPLOYED region=$AWS_REGION account=$AWS_ACCOUNT_ID"\n')
    (infra / "deploy-pipeline-infra.sh").chmod(0o755)

    def go(env_file=None, profile_region="", **env):
        if env_file is not None:
            (root / ".env").write_text(env_file)
        (bin_dir / "aws").write_text(f'#!/bin/bash\n[[ "$*" == "configure get region" ]] && echo "{profile_region}"\n')
        (bin_dir / "aws").chmod(0o755)
        base = {k: v for k, v in os.environ.items() if k not in ("AWS_REGION", "AWS_ACCOUNT_ID")}
        return subprocess.run(["bash", str(scripts / SCRIPT.name)], capture_output=True, text=True,
                              env={**base, "PATH": f"{bin_dir}:{os.environ['PATH']}", **env})
    return go


def test_region_from_the_aws_profile_and_account_from_the_env_file(run):
    r = run(env_file=f"CODE_REVIEW_URL=x\nAWS_ACCOUNT_ID={EXAMPLE}\n", profile_region="zz-profile-2")
    assert r.returncode == 0, r.stderr
    assert f"DEPLOYED region=zz-profile-2 account={EXAMPLE}" in r.stdout


def test_the_environment_wins_over_the_profile_and_the_env_file(run):
    r = run(env_file=f"AWS_ACCOUNT_ID={EXAMPLE}\n", profile_region="zz-profile-2",
            AWS_REGION="zz-test-1", AWS_ACCOUNT_ID=OTHER)
    assert r.returncode == 0, r.stderr
    assert f"DEPLOYED region=zz-test-1 account={OTHER}" in r.stdout


def test_no_region_anywhere_stops_the_deploy(run):
    r = run(env_file=f"AWS_ACCOUNT_ID={EXAMPLE}\n")
    assert r.returncode == 1 and "DEPLOYED" not in r.stdout
    assert "no AWS region: set AWS_REGION, or the region of your AWS profile" in r.stderr


def test_no_expected_account_anywhere_stops_the_deploy(run):
    r = run(env_file="CODE_REVIEW_URL=x\n", profile_region="zz-profile-2")
    assert r.returncode == 1 and "DEPLOYED" not in r.stdout
    assert "no expected AWS account: set AWS_ACCOUNT_ID" in r.stderr


def test_no_env_file_and_no_account_stops_the_deploy(run):
    r = run(profile_region="zz-profile-2")
    assert r.returncode == 1 and "no expected AWS account" in r.stderr
