#!/usr/bin/env bash
# alpha-mesh/test-apps/hello-b/hello.sh
# Prints "hello world" followed by today's date (ISO 8601, YYYY-MM-DD).

set -euo pipefail

echo "hello world $(date +%F)"
