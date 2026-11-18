#!/usr/bin/env bash
# alpha-mesh/test/test-no-aws-identifiers.sh
# PMAT-347: no AWS account ID or region is committed in the tracked tree.
# Scripts and the agent read both from the environment (AWS_REGION / the AWS profile, AWS_ACCOUNT_ID / .env).
#
# Allowed: 123456789012, AWS's documented example account (used by the CDK test).

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$MESH_DIR"

# A 12-digit number standing alone (not part of a hex SHA or a longer number).
ACCOUNT_RE='(^|[^0-9A-Za-z])[0-9]{12}([^0-9A-Za-z]|$)'
# Any AWS region name, e.g. us-east-1, eu-west-1, ap-southeast-2.
REGION_RE='\b(us|eu|ap|sa|ca|me|af|il|mx)-(north|south|east|west|central)(east|west)?-[0-9]\b'

FAILED=0

# Blank out the example account, then match again, so a line holding both still fails.
accounts="$(git grep -nIE "$ACCOUNT_RE" -- . ':!test/test-no-aws-identifiers.sh' \
  | sed -E 's/123456789012/EXAMPLE-ACCOUNT/g' | grep -E "$ACCOUNT_RE" || true)"
if [[ -n "$accounts" ]]; then
  echo "FAIL: AWS account IDs committed (read AWS_ACCOUNT_ID from the environment or .env instead):"
  echo "$accounts"
  FAILED=1
else
  echo "PASS: no AWS account ID in the tracked files"
fi

regions="$(git grep -nIE "$REGION_RE" -- . ':!test/test-no-aws-identifiers.sh' || true)"
if [[ -n "$regions" ]]; then
  echo "FAIL: AWS regions committed (read AWS_REGION or the AWS profile's region instead):"
  echo "$regions"
  FAILED=1
else
  echo "PASS: no AWS region in the tracked files"
fi

exit "$FAILED"
