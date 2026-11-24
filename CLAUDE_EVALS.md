# Evals and the merge queue (eval-rc-q)

Every alpha-mesh PR reaches master through one batch queue, `eval-rc-q/current`, which only tests: up to 4 queued
PRs are gated together (tests and the full evals once), failing PRs are dropped, and each passing PR is then
squash-merged into master on its own, one commit per ticket (PMAT-358). The runbook is `EVAL-RC-Q.md`.

## Workers
- Branch from `origin/master` and open the PR against master, as always. Its `evals-gate` check fails until the
  queue marks it `eval-rc-q` = success at its promotion: that is expected.
- An agent PR (one that changes a file `list_evals_gated_files.py` names) runs `mesh run-evals --smoke <PR url>`
  before `AWAITING-MERGE`; `mesh merge-pr` needs `evals-smoke` = success to queue it. Never run the full
  `mesh run-evals` on a ticket PR: the queue's gate runs it.
- The "ready to merge" comment ends with `mesh merge-pr <PR url>` as usual: it queues the PR.
- Once queued, never push to the PR, not even to merge master in: a queued PR that moves is dropped. This overrides
  the global rules to merge the default branch before every push and when a PR is stale, until the PR is merged or
  dropped. The ticket stays at `AWAITING-MERGE` until its PR is merged into master; then it is DONE, with the usual
  post-merge checks on `origin/master`.
- Dropped (a PR comment, and a note on the ticket): remove `AWAITING-MERGE`, fix it on the same branch (merge master
  in), run smoke again for an agent PR, then "ready to merge" again.
- Never post a commit status by hand: only `mesh eval-rc-q` posts `eval-rc-q` (and `evals` when it promotes), and
  only `mesh run-evals` posts `evals` and `evals-smoke`.
- A worker never gates, promotes or merges a hotfix.

## The queue (owner and team lead)
- One queue, up to 4 PRs. A batch with no agent file gates at once (tests only). Otherwise it gates when full
  (`mesh merge-pr` offers it), or at 00:30 UTC (`mesh eval-rc-q gate --due`) if it holds a PR. Nothing is queued while
  it gates.
- The gate never fixes the queue: it drops the failing PR and gates the rest again, until green. When no PR is to blame
  (the failing cases fail on master too, or only the judge's means dropped) it stops for the owner, dropping nothing.
- After a gate, post each pending note (`mesh eval-rc-q status` lists them) on its Jira ticket, as its first lines say,
  then delete it.
- One eval run at a time on the box; a run starts only when the free quota left covers it (400 full, 50 smoke).
- Hotfix, for an emergency only (a broken master, a security issue): a `hotfix/<TICKET>-…` branch, merged straight
  into master by the owner with `mesh merge-pr`, under the old eval rules. The next gate rebuilds the queue on it.

## Judge repeats
- Evals, like review passes (incremental while iterating, full before DONE): while iterating on the agent,
  the judge scores repeat 0 of each case only (`run_eval.sh --judge-repeats first`, the default; the free line
  checks still run on every repeat); the queue's gate scores all k (`--judge-repeats all`).
  The judge costs money per review; the repeats only add precision to the mean, which matters for the merge
  decision, not for seeing a change's direction.
