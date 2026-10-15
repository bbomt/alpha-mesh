#!/bin/bash
# Deploys the review-pull-request checkpoint stack (DynamoDB, S3, SSM) with CDK.
# Requires DEPLOY_ENV (dev|staging|prod) and AWS_REGION; see scripts/deploy-dev-from-ec2.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CDK_DIR="${SCRIPT_DIR}/../cdk"

case "${DEPLOY_ENV:-}" in
  dev|staging|prod) ;;
  *)
    echo "*** EXECUTION STOPPED: DEPLOY_ENV must be dev|staging|prod (got '${DEPLOY_ENV:-}')" >&2
    exit 1
    ;;
esac

if [[ -z "${AWS_REGION:-}" ]]; then
  echo "*** EXECUTION STOPPED: AWS_REGION is not set (e.g. export AWS_REGION=eu-west-1)" >&2
  exit 1
fi

CALLER_ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
if [[ -n "${AWS_ACCOUNT_ID:-}" && "${CALLER_ACCOUNT}" != "${AWS_ACCOUNT_ID}" ]]; then
  echo "*** EXECUTION STOPPED: credentials are for account ${CALLER_ACCOUNT}, expected ${AWS_ACCOUNT_ID}" >&2
  exit 1
fi

echo "[START] Deploying PmatAlphaMeshReviewCheckpointsStack-${DEPLOY_ENV} to ${AWS_REGION}"

cd "${CDK_DIR}"
pnpm install --frozen-lockfile
pnpm test
# cdk deploy has no --region option; the CLI takes the region from AWS_REGION
pnpm cdk:deploy -c config="${DEPLOY_ENV}"

echo "[DONE] SSM parameters:"
aws ssm get-parameters-by-path \
  --path "/pmat/${DEPLOY_ENV}/alpha-mesh/review-pull-request" \
  --region "${AWS_REGION}" \
  --query 'Parameters[].[Name,Value]' --output text
