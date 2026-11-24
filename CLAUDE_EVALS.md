# Evals and the merge queue (eval-rc-q)

Every alpha-mesh PR reaches master through one batch queue branch, `eval-rc-q/current`, whose gate runs the full
evals once per batch (PMAT-358). The runbook, with every command and its manual equivalent, is `EVAL-RC-Q.md`.

## Workers
- Branch from `origin/master` and open the PR against master, as always. Its `evals-gate` check fails while it
  targets master: that is expected, because `mesh merge-pr` moves it into the queue.
- An agent PR (one that changes a file `list_evals_gated_files.py` names) runs `mesh run-evals --smoke <PR url>`
  before `AWAITING-MERGE`; `mesh merge-pr` needs `evals-smoke` = success. Never run the full `mesh run-evals` on a
  ticket PR: the queue's gate runs it.
- "Merged" means merged into `eval-rc-q/current`; the ticket is DONE then. Run the post-merge tests on
  `origin/eval-rc-q/current` (on `origin/master` if the queue was already promoted and deleted).
- If the gate reverts the ticket, it reopens with the failing cases in a Jira comment: fix it on a new branch.
- A worker never gates, promotes, reverts on the queue or merges a hotfix.

## The queue (owner and team lead)
- One queue, up to 4 PRs. It gates at once when full (`mesh merge-pr` offers it), otherwise at 00:30 UTC
  (`mesh eval-rc-q gate --due`) if it holds a PR.
- One eval run at a time on the box; a run starts only when the free quota left covers it (400 full, 50 smoke).
- While the queue is `eval-rc:gating` or `eval-rc:fixing`, nothing merges into it: clear it first
  (`EVAL-RC-Q.md`, "Fix"). Never work around a blocked queue.
- Hotfix, for an emergency only (a broken master, a security issue): a `hotfix/<TICKET>-…` branch, merged straight
  into master by the owner with `mesh merge-pr`, under the old eval rules. The next gate merges it into the queue.

## Commit messages (the scope is the promotion's tag)
- Promotion: `chore[eval-rc-q-2026-11-24-1]: promoted PMAT-338, PMAT-347 (#72, #73)`.
- Master into the queue: `chore[eval-rc-q-2026-11-24-1]: merged master before the gate`.

## Judge repeats
- Evals, like review passes (incremental while iterating, full before DONE): while iterating on the agent,
  the judge scores repeat 0 of each case only (`run_eval.sh --judge-repeats first`, the default; the free line
  checks still run on every repeat); the queue's gate scores all k (`--judge-repeats all`).
  The judge costs money per review; the repeats only add precision to the mean, which matters for the merge
  decision, not for seeing a change's direction.
