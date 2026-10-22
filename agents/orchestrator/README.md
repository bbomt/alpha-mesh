# mesh orchestrate (PMAT-281 spike)

Runs one Claude session per Jira ticket, each in its own tmux tab, and does what the owner would otherwise
do by hand in those tabs.

- **Each Claude session is a worker.** It runs its ticket itself through the Claude harness (`CLAUDE.md`
  and the files it links): worktree, branch, labels, PR, review, "ready to merge", and Done after the
  merge. A session started by hand works the same way.
- **The orchestrator is the team lead.** It opens and reopens the workers' tabs, reads Jira, and types
  into a worker's tab what the owner would type. It never writes to Jira, so each ticket's labels have one
  writer: its worker.

## Usage

Run it in its own tmux tab, from the repo the workers should start in:

```bash
mesh orchestrate PMAT-282 PMAT-283   # start a worker per ticket, then keep watching
mesh orchestrate                     # after a reboot: reopen and resume the workers, then keep watching
mesh orchestrate --once              # one tick, then exit
```

Options: `--repo DIR` (default: the current folder), `--interval SECONDS` (default 30).

Each tick, per ticket:

| Seen | Done |
| --- | --- |
| The ticket's tab is missing (reboot, closed tab) | Reopen it and resume the worker (`claude --resume <id>`) in the folder it started in, telling it it was interrupted |
| A new owner comment (no `MESSAGE-FROM-WORKER` marker), whatever the labels: an answer, a rejected merge, feedback | Type "The owner commented on Jira …" into the tab, once per comment. Comments from before the worker started are not passed on |
| `AWAITING-MERGE`, and a merged PR has the ticket key in its title | Type "The PR … is merged. Finish the ticket as CLAUDE.md says." once |
| The ticket is Done after that | Close the tab |

It types into a tab only when the worker sits at its prompt (hook state `idle` or `waiting`). Then it
prints a table: ticket, tab, worker state, Jira status, labels.

**Orchestrated workers get a system prompt** (`worker-prompt.md`, passed with `--append-system-prompt-file`
on every start and resume). It is the only difference from a manual session:

- ask the owner on Jira (a comment, then `AWAITING-USER-RESPONSE`) and wait, taking the answer from Jira or the tab;
- start every Jira comment with `MESSAGE-FROM-WORKER` (workers and the owner post from the same Jira
  account, so a comment without the marker is taken as the owner's);
- work in a sibling worktree `<repo>-<TICKET>`, never in the checkout all workers start from.

Manual sessions ask in the chat only, so manual tickets get no extra Jira comments. The owner can take over
a worker by answering in its tab and telling it not to post to Jira.

## Files

- `worker-prompt.md`: the system prompt every orchestrated worker gets.
- `src/orchestrator/loop.py`: start a worker, one tick, the status table.
- `src/orchestrator/jira.py`: read-only Jira through the Atlassian MCP server (`https://mcp.atlassian.com/v2/mcp`).
- `src/orchestrator/status.py`: the per-ticket status files, `~/environment/logs/orchestrator/<KEY>.json`,
  shared with the hooks under the same lock.
- `src/orchestrator/tmux.py`: the tabs, named by ticket key, in the tmux session the orchestrator runs in.
- `smoke/`: the hooks (`hooks-settings.json`, `status-hook.sh`) every worker is started with, and the
  smoke test that shaped this design (`smoke/README.md`).
- Tests: `tests/` (fake tmux, gh and Jira), run by `test/test-orchestrator`.

## Atlassian login

The first run prints a link. Open it in a **fresh** browser tab (a reused tab fails with
`invalid_request`), choose the site, accept, and paste back the URL the browser ends on
(`http://localhost:8765/callback?...`; the page itself fails to load). The login is saved in
`~/.config/alpha-mesh/atlassian-oauth.json` (mode 600) and refreshed, so this is needed once.

The consent page lists every permission the server offers (about 30, all apps). It cannot be narrowed to
Jira: unticking narrows only by read/write/search across all apps, and a Jira-only request is rejected.
The orchestrator itself only reads.

## Limits (spike)

- Only tabs it started: a session started by hand is not watched.
- One repo per run (`--repo`); tickets in other repos need another orchestrator.
- Merges are found by ticket key in the PR title.
- Typing into a tab is one try; if the worker is busy, it waits for the next tick.
