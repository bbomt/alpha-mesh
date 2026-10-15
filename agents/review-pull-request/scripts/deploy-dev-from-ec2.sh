#!/bin/bash
# Deploys the review-pull-request infrastructure to dev from the EC2 instance (SSO dev-EC2).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

export DEPLOY_ENV=dev
export AWS_REGION=eu-west-1
export AWS_ACCOUNT_ID=609382076869

echo "=== DEPLOY_ENV=${DEPLOY_ENV} AWS_REGION=${AWS_REGION} AWS_ACCOUNT_ID=${AWS_ACCOUNT_ID}"

"${SCRIPT_DIR}/../infra/scripts/deploy-pipeline-infra.sh"
