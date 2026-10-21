#!/usr/bin/env bash
# PMAT-281 smoke test: a Claude Code hook that records a session's state in
# ~/environment/logs/orchestrator/$ORCH_TICKET.json, so an orchestrator never reads the screen.
# Usage (from the hook settings): status-hook.sh <state>; the hook input JSON arrives on stdin.
set -euo pipefail

state="$1"
ticket="${ORCH_TICKET:?ORCH_TICKET must name the ticket}"
dir="$HOME/environment/logs/orchestrator"
file="$dir/$ticket.json"
mkdir -p "$dir"

input="$(cat)"
[ -f "$file" ] || echo '{}' > "$file"

jq --arg state "$state" --arg ticket "$ticket" --arg at "$(date -Is)" --argjson input "$input" '
  . + {ticket: $ticket, state: $state, updated: $at,
       session_id: $input.session_id, cwd: $input.cwd,
       transcript_path: $input.transcript_path,
       event: $input.hook_event_name,
       notification_type: $input.notification_type, message: $input.message}
  | .history = ((.history // []) + [{state: $state, at: $at, event: $input.hook_event_name}])
  | if $input.hook_event_name == "SessionStart" then .session_start_input = $input else . end
' "$file" > "$file.tmp" && mv "$file.tmp" "$file"
