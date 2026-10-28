#!/usr/bin/env bash
# Marks the worker's Jira access as lost or working in ~/environment/logs/orchestrator/$ORCH_TICKET.json, so the
# orchestrator's screen can tell the owner to reconnect it (PMAT-303).
# Usage: jira-access.sh lost   (the worker, per worker-prompt.md, when its Atlassian tools stay gone)
#        jira-access.sh ok     (the PostToolUse hook in settings.json, after a working Atlassian call)
set -euo pipefail

case "${1:-}" in
  lost) lost="$(date -Is)" ;;
  ok) lost="" ;;
  *) echo "usage: jira-access.sh lost|ok" >&2; exit 2 ;;
esac
ticket="${ORCH_TICKET:?ORCH_TICKET must name the ticket}"
dir="$HOME/environment/logs/orchestrator"
file="$dir/$ticket.json"
mkdir -p "$dir"

# As the hook (ok) it is given the hook input on stdin, which may be large (the tool's response): read it all, so
# Claude's write never fails. The worker's call (lost) doesn't read stdin, which may be left open.
[ "$1" = lost ] || cat > /dev/null
# The same lock as status-hook.sh and the orchestrator, so no one's update is lost.
exec 9> "$file.lock"
flock 9
[ -f "$file" ] || echo '{}' > "$file"
jq --arg lost "$lost" '.jira_lost = (if $lost == "" then null else $lost end)' "$file" > "$file.tmp" \
  && mv "$file.tmp" "$file"
