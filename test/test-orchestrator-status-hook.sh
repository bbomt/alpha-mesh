#!/usr/bin/env bash
# alpha-mesh/test/test-orchestrator-status-hook.sh
# Tests for agents/orchestrator/status-hook.sh

set -euo pipefail

MESH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOOK="$MESH_DIR/agents/orchestrator/status-hook.sh"

# The hook writes under $HOME, so every run uses a throwaway HOME.
FAKE_HOME="$(mktemp -d)"
trap 'rm -rf "$FAKE_HOME"' EXIT
FILE="$FAKE_HOME/environment/logs/orchestrator/T-1.json"

FAILED=0
check() {
  local desc="$1" filter="$2"
  if jq -e "$filter" "$FILE" > /dev/null; then echo "PASS: $desc"; else echo "FAIL: $desc"; FAILED=1; fi
}
hook() { HOME="$FAKE_HOME" ORCH_TICKET=T-1 "$HOOK" "$1"; }

echo '{"session_id":"s-1","cwd":"/wt","transcript_path":"/t.jsonl","hook_event_name":"SessionStart","source":"startup"}' | hook started
check "SessionStart records the session ID and worktree" '.session_id == "s-1" and .cwd == "/wt" and .state == "started"'
check "SessionStart keeps the raw hook input" '.session_start_input.source == "startup"'

echo '{"session_id":"s-1","cwd":"/wt","hook_event_name":"UserPromptSubmit"}' | hook working
echo '{"session_id":"s-1","cwd":"/wt","hook_event_name":"Stop"}' | hook idle
echo '{"session_id":"s-1","cwd":"/wt","hook_event_name":"Notification","notification_type":"idle_prompt","message":"Claude is waiting for your input"}' | hook waiting
check "states are kept in order" '[.history[].state] == ["started","working","idle","waiting"]'
check "notification type is recorded" '.state == "waiting" and .notification_type == "idle_prompt"'
check "the start input survives later events" '.session_start_input.source == "startup"'

# Hooks of one session can fire at the same moment; no update may be lost.
rm -f "$FILE"
for i in $(seq 1 20); do
  echo "{\"session_id\":\"s-1\",\"cwd\":\"/wt\",\"hook_event_name\":\"Stop\"}" | hook idle &
done
wait
check "20 concurrent hooks keep all 20 history entries" '(.history | length) == 20'

# PMAT-303: jira-access.sh sets the "Jira lost" marker (the worker runs it) and clears it (the PostToolUse hook).
ACCESS="$MESH_DIR/agents/orchestrator/jira-access.sh"
access() { HOME="$FAKE_HOME" ORCH_TICKET=T-1 "$ACCESS" "$1" < /dev/null; }
echo '{"session_id":"s-1","cwd":"/wt","hook_event_name":"Stop"}' | hook idle
access lost
check "lost records when Jira was lost and keeps the state" '(.jira_lost | type) == "string" and .state == "idle"'
echo '{"session_id":"s-1","cwd":"/wt","hook_event_name":"UserPromptSubmit"}' | hook working
check "a later status hook keeps the marker" '(.jira_lost | type) == "string" and .state == "working"'
echo '{"tool_name":"mcp__atlassian__getJiraIssue"}' | HOME="$FAKE_HOME" ORCH_TICKET=T-1 "$ACCESS" ok
check "ok (as the PostToolUse hook, input on stdin) clears the marker" '.jira_lost == null and .state == "working"'

rm -f "$FILE"
for i in $(seq 1 10); do
  echo '{"session_id":"s-1","cwd":"/wt","hook_event_name":"Stop"}' | hook idle &
  access lost &
done
wait
check "concurrent status hooks and jira-access keep every update" '(.history | length) == 10 and (.jira_lost | type) == "string"'

if HOME="$FAKE_HOME" ORCH_TICKET=T-1 "$ACCESS" maybe < /dev/null 2> /dev/null; then
  echo "FAIL: jira-access refuses anything but lost or ok"; FAILED=1
else
  echo "PASS: jira-access refuses anything but lost or ok"
fi

# PMAT-323: a compact or /clear starts no turn, so no Stop would correct the state: they must not change it.
session_start() { echo "{\"session_id\":\"$2\",\"cwd\":\"/wt\",\"hook_event_name\":\"SessionStart\",\"source\":\"$1\"}" | hook started; }
OLD="2000-01-01T00:00:00+00:00"
for source in compact clear; do
  rm -f "$FILE"
  session_start startup s-1
  echo '{"session_id":"s-1","cwd":"/wt","hook_event_name":"Notification","notification_type":"idle_prompt","message":"Claude is waiting for your input"}' | hook waiting
  jq --arg old "$OLD" '.updated = $old' "$FILE" > "$FILE.tmp" && mv "$FILE.tmp" "$FILE"
  session_start "$source" s-2
  check "$source at the prompt keeps the state, time, notification type and message" \
    ".state == \"waiting\" and .updated == \"$OLD\" and .notification_type == \"idle_prompt\" and .message == \"Claude is waiting for your input\""
  check "$source still records the session and its input" ".session_id == \"s-2\" and .session_start_input.source == \"$source\""
  check "$source is in the history, with the state kept" '.history[-1].state == "waiting" and .history[-1].event == "SessionStart"'
  echo '{"session_id":"s-2","cwd":"/wt","hook_event_name":"UserPromptSubmit"}' | hook working
  session_start "$source" s-2
  check "$source in the middle of a turn stays working" '.state == "working"'
done
for source in startup resume; do
  echo '{"session_id":"s-1","cwd":"/wt","hook_event_name":"Notification","notification_type":"idle_prompt","message":"waiting"}' | hook waiting
  session_start "$source" s-1
  check "$source sets started and clears the notification" '.state == "started" and .notification_type == null and .message == null'
done

if echo '{}' | HOME="$FAKE_HOME" ORCH_TICKET= "$HOOK" idle 2> /dev/null; then
  echo "FAIL: a missing ORCH_TICKET is refused"; FAILED=1
else
  echo "PASS: a missing ORCH_TICKET is refused"
fi

exit "$FAILED"
