# EVAL-RC-Q: the batch queue runbook

Every alpha-mesh PR reaches master through one queue, `eval-rc-q/current`, which **only tests** (PMAT-358 v2):

```
ticket PR (against master, stays open)
   --mesh merge-pr-->        eval-rc-q/current = master + one commit per queued PR (up to 4)
   --mesh eval-rc-q gate-->  tests + full evals on the queue head, once for the batch
        fail: fluke check, bisect, drop the first bad PR, rebuild, gate again, until green
        pass: each PR in order: update it with master, check its tree is the gated tree, mark it
              eval-rc-q = success, squash-merge it into master (one commit per ticket)
```

This runbook is written to be followed with no other context. Every scripted step also has its manual equivalent in
`git` and `gh`, so the process still runs if a script breaks. The rules for agents are in `CLAUDE_EVALS.md`.

Run every command from the main checkout, `~/environment/pmat/alpha-mesh`, on an up-to-date master
(`mesh sync-main --repo-dir ~/environment/pmat/alpha-mesh`). `R=bbomt/alpha-mesh` below.

## The queue

- **The branch** `eval-rc-q/current`: master plus one commit per queued PR, in order. Each commit is the PR's diff
  (`git merge-tree`), its subject the PR's title and `(#N)`, with the trailers `PR: <n>` and `PR-Head: <sha>`. The
  trailers are the batch. The branch is disposable: the gate rebuilds it and force-pushes it; it is never merged.
- **The batch PR**: a draft PR from `eval-rc-q/current` into master, never merged, closed after the promotion. Its
  label is the queue's state; the gate's `evals` status and report are posted on it. Its own `evals-gate` check fails
  (it has no `eval-rc-q` status): expected.

| State | Label on the batch PR | What happens | Queuing |
| :-- | :-- | :-- | :-- |
| none | (no branch, no batch PR) | The next `mesh merge-pr` creates both. | yes |
| filling | `eval-rc:filling` | Up to 4 PRs are queued. | yes |
| gating | `eval-rc:gating` | Tests, full evals, drops and promotion. | **no** |

```bash
mesh eval-rc-q status
```
Expected: `Queue eval-rc-q/current: filling · 2/4 PRs · on master's tip`, then `Batch PR: <url>`, one line per PR
(`  #<n> at <sha7>: <title> (#<n>)`), and one line per Jira note still to post for a dropped PR.
Manual: `git fetch origin && git log --reverse --format='%(trailers:key=PR,valueonly) %s' origin/master..origin/eval-rc-q/current`
and `gh pr list -R $R --head eval-rc-q/current --state open --json url,labels`.

## Queue a ticket's PR

Before: the PR is open against master and reviewed. An agent PR has `evals-smoke` = success
(`mesh run-evals --smoke <PR url>`, run by the worker).

```bash
mesh merge-pr https://github.com/bbomt/alpha-mesh/pull/<n>
```
Expected: `Queued https://github.com/bbomt/alpha-mesh/pull/<n> (2/4 PRs in eval-rc-q/current, at <sha>). The PR stays open: do not push to it now.`
and `Batch PR: <url>`. At 4/4 it asks `Queue full: gate it now? [y/N]`: `y` gates now, `N` leaves it to tonight.
Check: `mesh eval-rc-q status` lists the PR; the PR is still open on GitHub, with no new commit.

When it did not work:

| It says | Do |
| :-- | :-- |
| `Not queued: the queue eval-rc-q/current is gating` | Wait for the gate to end; the PR waits at `AWAITING-MERGE`. Then run it again. |
| `Not queued: … is full (4/4 PRs)` | `mesh eval-rc-q gate`, then run it again after the promotion. |
| `BLOCKED  evals-smoke have not run on head …` | The worker runs `mesh run-evals --smoke <PR url>`. |
| `Not queued: the PR conflicts with the queue` | It waits for the next batch: after this batch is promoted, the worker merges master in, resolves, reruns the tests and pushes. |
| `Already queued` | Nothing to do. |

Manual equivalent (in the main checkout; no working tree is touched):
```bash
git fetch origin master eval-rc-q/current "+refs/pull/<n>/head:refs/eval-rc-q/pr/<n>"
tip=origin/eval-rc-q/current          # or origin/master when the queue does not exist yet
tree="$(git merge-tree --write-tree "$tip" refs/eval-rc-q/pr/<n> | head -n 1)"   # exit 1: a conflict, stop
c="$(printf '%s (#<n>)\n\nPR: <n>\nPR-Head: %s\n' "<PR title>" "$(git rev-parse refs/eval-rc-q/pr/<n>)" | git commit-tree "$tree" -p "$tip")"
git push --force-with-lease=refs/heads/eval-rc-q/current:"$(git rev-parse origin/eval-rc-q/current 2>/dev/null)" origin "$c:refs/heads/eval-rc-q/current"
# The first time only: the batch PR.
gh pr create -R $R --draft --base master --head eval-rc-q/current --label eval-rc:filling \
  --title "eval-rc-q: the queued batch (tests only, never merged)" --body "The queue eval-rc-q/current (EVAL-RC-Q.md)."
```

## Gate

```bash
mesh eval-rc-q gate
```
What it does, in order, with what it prints:
1. `Gating eval-rc-q/current: nothing is queued until it ends.` The label is now `eval-rc:gating`.
2. A PR that moved, closed or merged since it was queued is taken out (moved or closed: dropped). If anything changed,
   or master moved (a hotfix), the queue is rebuilt on master's tip: `Rebuilt eval-rc-q/current on master (<sha7>).`
3. `Batch: <n> PR(s) at <sha7>. Running the tests.` The repo's tests at the queue head, in a throwaway worktree. On a
   failure the tests run on the queue's commits (binary search) to find the first PR at which they fail; it is
   dropped and the gate goes back to step 2.
4. The full evals on the batch PR (`mesh run-evals <batch PR url>`: about 75 min, k=3, every repeat judged), unless no
   queued PR changes an agent file (`No queued PR changes an agent file: no evals needed.`).
5. On an evals failure, the drop loop:
   1. `The evals failed; cases that lost runs: <ids>.` (from the run's `regressed.json`).
   2. **Fluke check:** those cases only, k=3, on the same head (`mesh run-evals --cases … --run fluke-…`). They pass:
      `Fluke: the failing cases pass on a re-run. Gating again (attempt 2).`, a fresh full run, back to step 4. One
      fluke check per head.
   3. **Bisect:** those cases on the queue's commits (`--run bisect-…`), at most 2 runs for 4 PRs, to find the first
      PR at which they fail.
   4. **Drop** it: `Dropped #<n>: …` and `To post on Jira <ticket>: ~/.mesh/eval-rc-q/drops/<n>.md`. Back to step 2:
      the rest are rebuilt and gated again, with a full eval.
6. On a pass, **promote**, each PR in queue order: GitHub updates its branch with master (the master ruleset requires
   it up to date); its new tree must be the queue's tree at that PR; it gets the commit statuses `eval-rc-q` =
   success (and `evals` = success for an agent PR), a "Gated in eval-rc-q batch" comment, and is squash-merged:
   `Merged #<n> into master.` Last: `Promoted <n> PR(s) as eval-rc-q-<date>-<k>: master is at <sha>, the gated tree.`

Check: `git fetch && git log --oneline -5 origin/master` shows one squash commit per promoted ticket;
`git diff <gated sha> origin/master` is empty; the batch PR is closed; `mesh eval-rc-q status` says `No queue`.
Then `mesh sync-main --repo-dir ~/environment/pmat/alpha-mesh`, and post the drop notes (below).

When it did not work:

| It says (exit code) | State after | Do |
| :-- | :-- | :-- |
| `Gate paused … (exit 75 …)` (75) | gating | Another eval run holds the lock, or the free quota left is too low. `mesh eval-rc-q gate` again later, or leave it to tonight: it **resumes** the same head. |
| `Gate stopped: the evals failed but no case regressed` (1) | gating | The gate failed on the judge's means only, so no case points at a PR. Read the report on the batch PR; drop a PR by hand (Recovery) or rerun. |
| `Gate stopped without a verdict` (1) | gating | The eval run crashed (status `error`). Read its log, then `mesh eval-rc-q gate` again. |
| `The queue is empty: closed the batch PR …` (0 or 1) | none | Every PR was dropped (1) or merged (0). Post the drop notes. |
| `ERROR: master's tree … is not the gated tree` (1) | gating | Recovery. |

Manual equivalent of a pass (per PR, in queue order; `c` is the PR's queue commit):
```bash
gh api -X PUT repos/$R/pulls/<n>/update-branch -f expected_head_sha=<PR-Head>   # only if it is behind master
new="$(gh pr view <n> -R $R --json headRefOid --jq .headRefOid)"
[ "$(gh api repos/$R/commits/$new --jq .commit.tree.sha)" = "$(git rev-parse "$c^{tree}")" ] && echo same tree
gh api repos/$R/statuses/$new -f state=success -f context=eval-rc-q -f description="gated in batch eval-rc-q-<date>-<k> at <sha7>"
gh api repos/$R/statuses/$new -f state=success -f context=evals -f description="gated in batch …"   # agent PRs only
gh pr merge <n> -R $R --squash --match-head-commit "$new"
# After the last PR:
gh api repos/$R/git/refs -f ref=refs/tags/eval-rc-q-<date>-<k> -f sha="$(gh api repos/$R/git/ref/heads/master --jq .object.sha)"
gh pr close <batch PR> && git push origin --delete eval-rc-q/current
```

## Promote only

`mesh eval-rc-q promote` promotes a batch whose evals already passed on its head (for example after a crash during the
promotion). It refuses if the batch changed or the evals have not passed: then run `mesh eval-rc-q gate`.

## Dropped PRs and their Jira notes

The gate has no Jira access. For each dropped PR it comments on the PR and writes `~/.mesh/eval-rc-q/drops/<n>.md`,
whose first line names the ticket. The team lead posts each note as a comment on that ticket, so the worker sees it,
then deletes the file. `mesh eval-rc-q status` lists the notes still to post.

## Nightly

The cron line (installed in the owner's crontab, `crontab -e`). 00:30 UTC: the free-model quota counts per UTC day.
```
30 0 * * * cd $HOME/environment/pmat/alpha-mesh && PATH=$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin setsid nohup bin/mesh eval-rc-q gate --due >> $HOME/environment/logs/ai/claude/eval-rc-q-nightly.log 2>&1 < /dev/null
```
`gate --due` gates a filling queue that holds a PR and resumes one left `eval-rc:gating`.
Logs: `~/environment/logs/ai/claude/eval-rc-q-nightly.log` (outside `/tmp`, so a reboot keeps it), and the quota before
and after every eval run in `~/.mesh/quota.log`.
Check in the morning: `tail -n 30 ~/environment/logs/ai/claude/eval-rc-q-nightly.log`, `mesh eval-rc-q status`, then post
any drop notes.

## Hotfix: the one exception

For a real emergency only: a broken master, or a security issue. A branch named `hotfix/<TICKET>-<words>`, from
master, PR against master. Only the owner merges it:
```bash
mesh merge-pr <hotfix PR url>
```
It merges straight into master, under the old rules: up to date with master, and an agent hotfix needs the full
`evals` status on its head (`mesh run-evals <PR url>`). `evals-gate` asks no `eval-rc-q` status of it. The next gate
rebuilds the queue on the new master.

## Recovery

- **A gate stopped by the quota, a reboot or a killed job:** the label stays `eval-rc:gating`, so nothing is queued and
  the head is unchanged. `mesh eval-rc-q gate` (or tonight's `gate --due`) resumes: `mesh run-evals` continues the same
  run from its checkpoints in `~/.mesh/eval-runs/`. If a cron job was killed, post a Jira comment on each queued
  ticket saying the gate did not finish.
- **Drop a PR by hand:** take it out and rebuild:
  ```bash
  git fetch origin master eval-rc-q/current
  # rebuild from origin/master with every queued PR but <n>, as in "Queue a ticket's PR" (manual), then push
  ```
  comment on the PR, post the note on its ticket, and `mesh eval-rc-q gate`.
- **`ERROR: master's tree at <sha> is not the gated tree`:** master moved during the promotion (a hotfix merged while
  gating). Nothing is tagged; the branch and batch PR are kept. Compare `git diff <gated sha> origin/master`; if it is
  only the hotfix, `mesh eval-rc-q gate` again (it rebuilds on master and gates what is left); otherwise tell the owner.
- **A PR's tree differs after GitHub's update-branch** (should not happen: GitHub's merge and `git merge-tree` agree on a
  clean merge): that PR is dropped at its turn, and the PRs before it are already on master. They were gated together
  with the dropped PR but not without it, so check master's next gate closely and tell the owner.
