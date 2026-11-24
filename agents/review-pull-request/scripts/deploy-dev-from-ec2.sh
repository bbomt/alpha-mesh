#!/bin/bash
# Deploys the review-pull-request infrastructure to dev from the EC2 instance (SSO dev-EC2).
# No account or region is committed (PMAT-347):
#   - the region is AWS_REGION, else the region of your AWS profile (aws configure get region);
#   - the expected account is AWS_ACCOUNT_ID, from the environment or else the alpha-mesh .env (gitignored).
# Either one missing stops the deploy: the account check is what keeps a deploy out of the wrong account.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../../../.env"

export DEPLOY_ENV=dev

if [[ -z "${AWS_REGION:-}" ]]; then
  AWS_REGION="$(aws configure get region 2>/dev/null || true)"
fi
if [[ -z "${AWS_REGION}" ]]; then
  echo "*** EXECUTION STOPPED: no AWS region: set AWS_REGION, or the region of your AWS profile (aws configure set region <region>)" >&2
  exit 1
fi
export AWS_REGION

if [[ -z "${AWS_ACCOUNT_ID:-}" && -f "${ENV_FILE}" ]]; then
  AWS_ACCOUNT_ID="$(sed -n 's/^AWS_ACCOUNT_ID=//p' "${ENV_FILE}" | tail -n 1)"
fi
if [[ -z "${AWS_ACCOUNT_ID:-}" ]]; then
  echo "*** EXECUTION STOPPED: no expected AWS account: set AWS_ACCOUNT_ID in the environment or in ${ENV_FILE}" >&2
  exit 1
fi
export AWS_ACCOUNT_ID

echo "=== DEPLOY_ENV=${DEPLOY_ENV} AWS_REGION=${AWS_REGION} AWS_ACCOUNT_ID=${AWS_ACCOUNT_ID}"

"${SCRIPT_DIR}/../infra/scripts/deploy-pipeline-infra.sh"
