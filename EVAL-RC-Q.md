# EVAL-RC-Q: the batch queue runbook

Every alpha-mesh PR reaches master through one queue branch, `eval-rc-q/current`. Up to 4 PRs are squash-merged
into it; its gate runs the tests and the full review-pull-request evals once for the whole batch; a pass promotes
it into master with one merge commit (PMAT-358). This runbook is written to be followed with no other context.
Every scripted step also has its manual equivalent in `git` and `gh`, so the process still runs if a script breaks.
The rules for agents are in `CLAUDE_EVALS.md`.

Run every command from the main checkout, `~/environment/pmat/alpha-mesh`, on an up-to-date master
(`mesh sync-main --repo-dir ~/environment/pmat/alpha-mesh`). `R=bbomt/alpha-mesh` below.

## The queue's life cycle

| State | Label on the promotion PR | What happens | Merges into the queue |
| :-- | :-- | :-- | :-- |
| none | (no branch) | The next `mesh merge-pr` creates the branch from master. | yes |
| filling | `eval-rc:filling` | Up to 4 PRs merge in. | yes |
| gating | `eval-rc:gating` | Master is merged in if it moved, then the tests, then the full evals. | **no** |
| fixing | `eval-rc:fixing` | The gate failed; the team clears it (Fix), then gates again. | **no** |
| promoted | (branch deleted) | Master holds the batch, tagged `eval-rc-q-YYYY-MM-DD-<n>`. | (next merge: new queue) |

The state lives on GitHub only: the branch and its one open **draft promotion PR** into master. Its commits are
the batch (one squash commit per ticket); its label is the state; the gate's `evals` status and report are on it.

```bash
mesh eval-rc-q status
```
Expected: `Queue eval-rc-q/current: filling · 2/4 PRs · master moved since: 0 commit(s)`, the promotion PR's link and
one line per ticket, or `No queue: eval-rc-q/current does not exist.`
Manual: `gh pr list -R $R --head eval-rc-q/current --state open --json url,labels` and
`gh api repos/$R/compare/master...eval-rc-q/current --jq '.commits[].commit.message | split("\n")[0]'`.

## Fill: merge a ticket's PR into the queue

Before: the PR is open against master and reviewed. An agent PR has `evals-smoke` = success
(`mesh run-evals --smoke <PR url>`, run by the worker).

```bash
mesh merge-pr https://github.com/bbomt/alpha-mesh/pull/<n>
```
Expected: `Merged https://github.com/bbomt/alpha-mesh/pull/<n> into the queue eval-rc-q/current (squash, at <sha>): 2/4 PRs.`
and `Promotion PR: <url>`. The first merge after a promotion also prints `Created the queue eval-rc-q/current from master`.
At 4/4 it asks `Queue full: gate it now? [y/N]`: answer `y` to gate now (Gate, below), `N` to leave it to tonight.

Check: `mesh eval-rc-q status` lists the ticket; the PR shows as merged into `eval-rc-q/current` on GitHub.

When it did not work:

| It says | Do |
| :-- | :-- |
| `the queue eval-rc-q/current is gating` / `is fixing: clear it first` | Nothing merges now. The PR waits at `AWAITING-MERGE`; clear the queue (Gate or Fix), then run it again. |
| `is full (4/4 PRs): gate it first` | `mesh eval-rc-q gate`, then run it again after the promotion. |
| `BLOCKED  evals-smoke have not run on head …` | The worker runs `mesh run-evals --smoke <PR url>`. |
| `conflicts with the queue` | The base is back on master. The worker merges `origin/eval-rc-q/current` into the branch, resolves, reruns the tests, pushes; then run it again. |

Manual equivalent:
```bash
# Only if `gh api repos/$R/branches/eval-rc-q/current` says 404: create the queue from master.
gh api repos/$R/git/refs -f ref=refs/heads/eval-rc-q/current -f sha="$(gh api repos/$R/git/ref/heads/master --jq .object.sha)"
gh pr edit <PR url> --base eval-rc-q/current
gh pr view <PR url> --json mergeable          # MERGEABLE (wait and repeat while UNKNOWN)
gh pr merge <PR url> --squash --match-head-commit "$(gh pr view <PR url> --json headRefOid --jq .headRefOid)"
# Only after the first merge into a new queue (GitHub refuses a PR with no commits):
gh pr create -R $R --draft --base master --head eval-rc-q/current --label eval-rc:filling \
  --title "eval-rc-q: promote the queued batch into master" --body "The queue eval-rc-q/current (EVAL-RC-Q.md)."
```

## Gate: test and evaluate the batch, then promote it

```bash
mesh eval-rc-q gate
```
What it does, in order (each line below is what it prints):
1. `Gating eval-rc-q/current (<n> PRs): merges into the queue are frozen.` The label is now `eval-rc:gating`.
2. `Merged master into eval-rc-q/current.` Only when a hotfix moved master. Commit message:
   `chore[eval-rc-q-<date>-<n>]: merged master before the gate`.
3. `Gated head: <sha>. Running the tests.` The repo's tests (`test/run-all.sh`) at the gated head, in a throwaway
   worktree.
4. The full evals (`mesh run-evals <promotion PR url>`: about 75 min, k=3, every repeat judged), unless no PR in the
   batch changes an agent file: then `The evals are not needed (no agent file in the batch)`.
5. On a pass: Promote (below). Expected last lines: `Promoted: chore[eval-rc-q-<date>-<n>]: promoted PMAT-…, PMAT-… (#…, #…)`
   and `master is at <sha> (tag eval-rc-q-<date>-<n>); eval-rc-q/current is deleted`.

Check: `git fetch && git log --first-parent -1 origin/master` is the promotion; `git diff <gated sha> origin/master`
is empty; `mesh eval-rc-q status` says `No queue`. Then `mesh sync-main --repo-dir ~/environment/pmat/alpha-mesh`.

When it did not work:

| It says (exit code) | State after | Do |
| :-- | :-- | :-- |
| `Gate paused before the evals finished (exit 75 …)` (75) | gating | Another eval run holds the lock, or the free quota left is too low. Run `mesh eval-rc-q gate` again later, or leave it to tonight: it **resumes** the same head (Recovery). |
| `Gate failed: Merging master into eval-rc-q/current conflicts.` (1) | fixing | Recovery: a conflict merging master. |
| `Gate failed: The tests failed on <sha>.` (1) | fixing | Fix, from step 2 (the tests' output names the ticket). |
| `Gate failed: The full evals failed on <sha>` (1) | fixing | Fix. The report is the `mesh run-evals` comment on the promotion PR. |
| `Gate stopped without a verdict` (1) | gating | The eval run crashed (its status is `error`). Read its log, then `mesh eval-rc-q gate` again. |

Manual equivalent:
```bash
gh pr edit <promotion PR> --add-label eval-rc:gating --remove-label eval-rc:filling --remove-label eval-rc:fixing
gh api -X POST repos/$R/merges -f base=eval-rc-q/current -f head=master \
  -f commit_message="chore[eval-rc-q-<date>-<n>]: merged master before the gate"    # only if master moved
git fetch origin eval-rc-q/current && git worktree add --detach ~/environment/pmat/alpha-mesh-run-evals/tests origin/eval-rc-q/current
(cd ~/environment/pmat/alpha-mesh-run-evals/tests && bash test/run-all.sh); git worktree remove --force ~/environment/pmat/alpha-mesh-run-evals/tests
mesh run-evals <promotion PR url>      # expected last line: Gate passed: evals = success on <sha>.
```

## Promote

`mesh eval-rc-q gate` promotes on a pass. To promote a queue whose evals already passed (for example after a
crash between the evals and the merge):

```bash
mesh eval-rc-q promote
```
It refuses (`Not promoted: the evals have not passed …`) unless the promotion PR is cleared by `check_pr_evals.py`.
Then it marks the PR ready, waits for its required checks, merges it with a **merge commit** at the gated sha, checks
master's tree is the gated tree, tags the merge commit, moves any PR still targeting the queue back to master and
deletes the branch.

Manual equivalent:
```bash
sha="$(gh pr view <promotion PR> --json headRefOid --jq .headRefOid)"
gh pr ready <promotion PR>
gh pr checks <promotion PR> --required --watch
gh pr merge <promotion PR> --merge --match-head-commit "$sha" \
  --subject "chore[eval-rc-q-<date>-<n>]: promoted PMAT-a, PMAT-b (#x, #y)"
m="$(gh pr view <promotion PR> --json mergeCommit --jq .mergeCommit.oid)"
[ "$(gh api repos/$R/commits/$m --jq .commit.tree.sha)" = "$(gh api repos/$R/commits/$sha --jq .commit.tree.sha)" ] && echo same tree
gh api repos/$R/git/refs -f ref=refs/tags/eval-rc-q-<date>-<n> -f sha="$m"   # <n>: 1 + that day's tags so far
gh api -X DELETE repos/$R/git/refs/heads/eval-rc-q/current
```

Why a merge commit: master's tree is exactly the gated tree; each ticket's squash commit stays on master
(`git revert <ticket sha>` undoes one ticket); `git log --first-parent master` lists the promotions;
`git bisect --first-parent` finds a bad promotion; `git revert -m 1 <promotion sha>` undoes a whole batch.

## Nightly

The cron line (installed in the owner's crontab, `crontab -e`). 00:30 UTC: the free-model quota counts per UTC day.
```
30 0 * * * cd $HOME/environment/pmat/alpha-mesh && PATH=$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin setsid nohup bin/mesh eval-rc-q gate --due >> $HOME/environment/logs/ai/claude/eval-rc-q-nightly.log 2>&1 < /dev/null
```
`gate --due` gates a filling queue that holds at least one PR, resumes a queue left `eval-rc:gating`, and leaves a
queue being fixed alone (`Nothing due: the queue is fixing with <n> PR(s).`).
Logs: `~/environment/logs/ai/claude/eval-rc-q-nightly.log` (outside `/tmp`, so a reboot keeps it), and the quota
before and after every eval run in `~/.mesh/quota.log`.
Check in the morning: `tail -n 20 ~/environment/logs/ai/claude/eval-rc-q-nightly.log` and `mesh eval-rc-q status`.

## Hotfix: the one exception

For a real emergency only: a broken master, or a security issue. A branch named `hotfix/<TICKET>-<words>`, from
master, PR against master. Only the owner merges it:
```bash
mesh merge-pr <hotfix PR url>
```
It merges straight into master, under the old rules: up to date with master, and an agent hotfix needs the full
`evals` status on its head (`mesh run-evals <PR url>`). The next gate merges master, with the hotfix, into the queue.

## Fix: clear a queue labelled eval-rc:fixing

Fix stays manual until it has run 3 times with no change to this text. Each time, post a Jira comment on PMAT-358
with the steps run, the time taken, how long PRs waited at `AWAITING-MERGE`, and any deviation from this text.

Only the owner or the team lead fixes the queue; an orchestrated worker never reverts.

1. **Fluke or regression.** Re-run only the failing cases, N times, on the gated head, in a scratch worktree:
   `eval/scripts/run_eval.sh --run fix-<sha7> --k 3 --filter-pattern '^(<case>|<case>)$'` (from
   `agents/review-pull-request`). Judge them with PMAT-312's noise tolerance (`score_eval_run.py`), not a made-up
   rule. Infrastructure failures (timeouts, 429s, provider errors: PMAT-330) are not `model_failure`; rerun those.
   Output: the case IDs that fail reproducibly. None: `mesh eval-rc-q gate` again.
2. **Map the failing cases to the changed files.** From each failing case's trace, find which part degraded (prompt,
   stage, tool or reader). `git log --stat $(git merge-base origin/master origin/eval-rc-q/current)..origin/eval-rc-q/current -- agents/review-pull-request/`
   lists one squash commit per ticket. Output: 1-3 candidate tickets.
3. **Bisect on the failing cases only,** in a scratch worktree, never the main checkout:
   `git bisect start <gated sha> <merge-base>`, each step running only the reproducible failing cases. 4 tickets take
   at most 2 steps. Output: the first bad commit, its PR and its ticket.
4. **Revert on the queue, never on master:**
   ```bash
   git fetch origin eval-rc-q/current && git worktree add ~/environment/pmat/alpha-mesh-fix origin/eval-rc-q/current
   cd ~/environment/pmat/alpha-mesh-fix && git switch -c fix-queue && git revert --no-edit <squash sha>
   git push origin HEAD:eval-rc-q/current && cd - && git worktree remove ~/environment/pmat/alpha-mesh-fix
   ```
   Comment on the ticket with the failing cases, then reopen it. Add the caught cases to `eval/smoke.json`, retiring a
   redundant one to keep it at 15-20 cases. Then gate again: `mesh eval-rc-q gate`.
5. **Hold-out cases are never opened.** The gate reports hold-out totals only; fix and bisect use development cases
   only. A regression seen only in the hold-out is reported to the owner as a total.

## Recovery

- **A gate stopped by the quota, a reboot or a killed job:** the label stays `eval-rc:gating` and nothing can merge,
  so the head is unchanged. Run `mesh eval-rc-q gate` again (or let `gate --due` do it tonight): `mesh run-evals`
  resumes the same run (`gate-pr<promotion PR>-<sha7>`) from its checkpoints in `~/.mesh/eval-runs/`, reusing every
  finished review.
- **A conflict merging master into the queue:** in a scratch worktree of `origin/eval-rc-q/current`,
  `git merge origin/master -m "chore[eval-rc-q-<date>-<n>]: merged master before the gate"`, resolve, run the
  tests, `git push origin HEAD:eval-rc-q/current`, then `mesh eval-rc-q gate`.
- **A ticket PR that conflicts with the queue:** see Fill.
- **Every PR in the batch reverted:** close the promotion PR (`gh pr close <promotion PR>`) and delete the branch
  (`gh api -X DELETE repos/$R/git/refs/heads/eval-rc-q/current`). The next merge starts a new queue.
- **`ERROR: master's tree at <sha> is not the gated tree`:** master moved during the promotion (a hotfix merged while
  gating). Not tagged; the branch is kept. Compare `git diff <gated sha> origin/master`; if it is only the hotfix,
  tag the merge commit by hand (Promote, manual) and delete the branch; otherwise tell the owner.
- **A cron job killed:** post a Jira comment on each ticket in the batch saying the gate did not finish, then run
  `mesh eval-rc-q gate` (it resumes).
