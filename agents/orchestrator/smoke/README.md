# PMAT-281 smoke test: steps 0–11

The hand-run checks from PMAT-281's smoke test plan, run on 2026-10-21 and 22 before any orchestrator is built.
Claude Code 2.1.306, tmux 3.2a.

## Files

- `status-hook.sh <state>`: a Claude Code hook. It reads the hook input on stdin and writes the session's
  state to `~/environment/logs/orchestrator/$ORCH_TICKET.json` (state, session ID, worktree, transcript,
  notification type, history; the full `SessionStart` input is kept as `session_start_input`).
- `hooks-settings.json`: the hooks, passed to `claude --settings <file>`:
  `SessionStart → started`, `UserPromptSubmit → working`, `Stop → idle`, `Notification → waiting`.
- Test: `test/test-orchestrator-status-hook`.

How a ticket's session was started (in the ticket's worktree):

```bash
tmux new-window -d -n SMOKE-1 -c ../alpha-mesh-smoke \
  "ORCH_TICKET=SMOKE-1 claude --name SMOKE-1 --session-id <uuid> --settings <hooks-settings.json> '<prompt>'; exec bash"
# recovery
tmux new-window -d -n SMOKE-1 -c ../alpha-mesh-smoke \
  "ORCH_TICKET=SMOKE-1 claude --resume <uuid> --settings <hooks-settings.json> '<nudge>'; exec bash"
```

## Results

| # | Step | Result |
| --- | --- | --- |
| 0 | Tools and access | **Pass, with a decision.** tmux 3.2a, Claude Code 2.1.306, `gh` logged in (it needs network to `api.github.com`; inside Claude's sandbox that host must be allowed). There is no Jira API token: Jira is reached only through the Atlassian MCP connector (owner, 2026-10-21). So a plain script cannot read or write Jira; that work goes through a Claude session that has the connector. |
| 1 | Worktree | **Pass.** `../alpha-mesh-smoke` on `test/PMAT-281-smoke`. |
| 2 | tmux window + `claude "<prompt>"` | **Pass.** Window `SMOKE-1` showed in `tmux list-windows`; `hello.txt` (`hi`) was created 10 s later with nobody typing. No allow-list was needed: sessions inherit the user's `permissionMode: auto`. |
| 3 | `SessionStart` hook saves the session ID | **Pass.** The status file held the session ID and the worktree path. The hook was passed with `--settings`, so no Claude settings file was edited and no owner approval was needed. |
| 4 | Naming | **Pass.** `claude --name SMOKE-1` exists; the transcript records `custom-title: SMOKE-1`, which the `/resume` picker shows. `--resume` also accepts a name. No `/rename` typing needed. |
| 5 | Status from hooks | **Pass.** `idle` on `Stop`, and `waiting` on `Notification`. The idle notification (`notification_type: idle_prompt`) fires 60 s after a turn ends, so `waiting` alone does not mean "Claude asked something"; the type tells them apart. |
| 6 | Kill when idle, resume | **Pass.** `claude --resume <id>` kept the **same** session ID (no fork); the hook saw `source: resume`; asked "what file did you create?", it answered `hello.txt`. |
| 7 | Kill mid-turn, resume with the nudge | **Pass.** Killed after f1–f2 of 5 files. Resumed with the nudge, it checked `git status`, saw the interrupted command (exit 137), reran it, and wrote only f3–f5; f1–f2 kept their original times. |
| 8 | Real reboot mid-turn (2026-10-22) | **Pass.** The overnight stop and a reboot by the owner mid-task (g1–g4 of 5 written, 30 s sleeps) both kept the worktree, the transcript and the status file; the status file still said `working` (stale, as expected). Resumed in a new window with `--resume <id>` and the nudge, it saw the interrupted sleep (exit 144), reran it, and wrote only g5; g1–g4 kept their times. Asked which file it created first, it answered `hello.txt`, so the context survived the reboot. |
| 9 | Two label adds at once | **Fail (as predicted).** Through the MCP (no plain-script Jira, step 0), the only label write is `editJiraIssue`, which sets the whole list; the connector has no add/remove operation, and `transitionJiraIssue`'s `update` needs a status change. Two near-simultaneous writers, each adding its label to the `[ALPHA-MESH]` it had read, ended with `[ALPHA-MESH, SMOKE-TEST-B]`: A's label was lost. Labels were restored to `[ALPHA-MESH]`. |
| 10 | Question through Jira | **Pass, after a fix.** First try: the worktree session had **no Atlassian connector**, because the plugin is enabled only in alpha-mesh's gitignored `.claude/settings.json`, which a new worktree does not have. Adding `enabledPlugins` to `hooks-settings.json` fixed it. Then the session posted the question comment, set `[ALPHA-MESH, AWAITING-USER-RESPONSE]`, and waited; the answer (`green`, typed into the window with `tmux send-keys`) made it remove only that label and write `colour.txt`. The session noticed the step 9 race itself. |
| 11 | PR polling | **Pass.** `gh pr view 19 --json state` → `MERGED`; `21` → `CLOSED`. |

## What this changes in the design

1. **The orchestrator picks the session ID.** `claude --session-id <uuid>` sets it up front, so the
   orchestrator writes the ID to the ticket's status file before starting Claude. The `SessionStart` hook
   then only confirms it. Recovery does not depend on the hook having run.
2. **Hooks via `--settings`, not settings files.** One shared hooks file, passed on the command line; the
   ticket key reaches the hook as `ORCH_TICKET` in the environment.
3. **Name via `--name`.** No `/rename` sent into the window.
4. **Status files go stale on a kill.** Killing the window fires no hook, so a killed session's file still
   shows its last state (step 7 showed `idle` while it was mid-task, before `UserPromptSubmit → working`
   was added). The orchestrator must check that the tmux window and process exist, not trust the file alone.
5. **`waiting` needs its type.** `idle_prompt` (finished, idle for 60 s) and a permission prompt are both
   `Notification`; use `notification_type`.
6. **Typing into a window works.** A prompt sent with `tmux send-keys -l '<text>'` then `Enter` was taken
   as a normal prompt (step 7). One try only; fragility for v2 is still to be measured.
7. **Jira only through a Claude session** (step 0). The "deterministic script polls Jira labels" part needs
   either a Jira token after all or a Claude call per poll; to be settled before building the orchestrator.
8. **Labels need one writer at a time** (step 9). With replace-only writes, the orchestrator must not write a
   ticket's labels while that ticket's session is live; post-merge label removal happens only after the
   session's window is closed. A Jira API token would allow REST `update: {labels: [{add|remove}]}` instead.
9. **Sessions need the plugins passed in** (step 10). Worktree sessions get only user-level plugins; the
   `--settings` file must enable the Atlassian plugin (and any other project plugin a ticket needs).
10. **`AWAITING-USER-RESPONSE` does not show in the status file.** While waiting for the answer the file said
    `idle`, then `waiting` (idle_prompt) after 60 s; the Jira label is the signal for "asked the owner".

## Re-running step 8

A reboot kills the tmux server, so start the mid-turn task **before** the owner reboots: recreate the window
with `claude --resume <id>` and a slow task, wait until part of it is done and the status file says
`working`, then reboot. Use sleeps of minutes, not 30 s: with 30 s there were about 90 seconds to reboot
before the task finished.

## Still to run

Step 12 (AWS, owner) and 13 (cleanup of the worktree `../alpha-mesh-smoke`, branch `test/PMAT-281-smoke`, the
`SMOKE-1` window and `~/environment/logs/orchestrator/SMOKE-1.json*`).
