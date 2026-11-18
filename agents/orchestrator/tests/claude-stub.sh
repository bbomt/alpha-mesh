#!/usr/bin/env bash
# Stand-in for `claude` in the end-to-end test. Records how it was started, fires the real status hook as
# Claude would (SessionStart, then a turn for the prompt it was given), then logs each line typed into its
# tab and fires a turn for it.
set -euo pipefail

key="$ORCH_TICKET"
printf '%s\t%s\n' "$PWD" "$*" >> "$E2E_DIR/$key.argv"

session="" source="startup"
args=("$@")
for i in "${!args[@]}"; do
  case "${args[$i]}" in
    --session-id) session="${args[$((i + 1))]}" ;;
    --resume) session="${args[$((i + 1))]}"; source="resume" ;;
  esac
done

hook() {  # hook <state> <event>
  printf '{"session_id":"%s","cwd":"%s","hook_event_name":"%s","source":"%s"}' \
    "$session" "$PWD" "$2" "$source" | "$E2E_HOOK" "$1"
}
turn() { hook working UserPromptSubmit; hook idle Stop; }

hook started SessionStart
turn  # the prompt given on the command line
while IFS= read -r line; do
  printf '%s\n' "$line" >> "$E2E_DIR/$key.typed"
  turn
done
