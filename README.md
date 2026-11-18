# alpha-mesh

The memory and orchestration layer for the PMAT agent system.

## Structure

```
alpha-mesh/
├── bin/                     # Bash scripts (install.sh symlinks only mesh into ~/bin)
│   ├── mesh                 # Unified CLI entry point (wraps all commands)
│   ├── model.sh             # Switch/check model config
│   ├── dream.sh             # End-of-session memory consolidation (delegates to agents/dream/)
│   ├── review-pull-request.sh # PR review agent (delegates to agents/review-pull-request/)
│   ├── run-evals.sh         # Runs the review-pull-request evals on a PR (or --quick, --force-success); posts `evals`
│   ├── merge-pr.sh          # The owner's merge: refuses a stale PR branch, or an agent PR without passing evals
│   ├── sync-main.sh         # Fast-forwards a main checkout and verifies it, or fails loudly
│   ├── orchestrate.sh       # One Claude worker per Jira ticket in tmux tabs (delegates to agents/orchestrator/)
│   ├── bootstrap-memory     # One-time: extract from existing Claude Code logs
│   ├── install.sh           # One-time setup
│   └── lib/repo.sh          # Shared bash functions: find_repo_root, get_active_model
├── agents/
│   ├── dream/               # Dream agent
│   │   ├── dream/           # Python package
│   │   │   ├── __init__.py  # All dream logic: session extraction, LLM calls, memory synthesis
│   │   │   ├── __main__.py  # Entry point for: python3 -m dream
│   │   │   └── bin/dream.sh # Shell entry point
│   │   └── tests/
│   │       └── test_dream_core.py  # Python tests for dream module
│   ├── orchestrator/        # mesh orchestrate — the mesh orchestrator for per-ticket Claude workers (PMAT-281)
│   └── review-pull-request/ # PR review agent — LLM-powered, two-stage review (see below)
├── config/
│   └── models.json          # All model profiles
├── memory/
│   ├── sessions/            # Historical dream output (legacy — no longer written)
│   ├── learnings/           # Historical learnings (legacy — no longer written)
│   └── skills/              # Extracted workflow skills (reusable prompts/procedures)
├── test/
│   ├── run-all.sh           # Runs all test suites
│   ├── test-dream.sh        # Tests for bin/dream.sh
│   ├── test-model.sh        # Tests for bin/model.sh
│   └── test-install.sh      # Tests for bin/install.sh
├── pyproject.toml           # Python project definition (uv)
├── uv.lock                  # Locked dependencies
├── .python-version          # Python version pin
└── .gitignore
```

Dream output goes to `{REPO}/.mesh/MEMORY.md` in each repo worked in.
Cross-repo learnings go to `~/.mesh/MEMORY.md`.

## Quick Start

```bash
# Install (run once)
cd ~/environment/pmat
git clone <this-repo> alpha-mesh
cd alpha-mesh && ./bin/install.sh

# The `mesh` CLI is now available globally — prefix for all commands. install.sh links only
# `mesh` into ~/bin (and removes the per-command links older installs made): run `mesh model`, not `model`.
# Every script under bin/ ends in .sh except bin/mesh, the one command.
# Switch model (prompts for scope — L: local .claude/settings.json, G: global ~/.claude/settings.json)
# Local scope walks up from CWD to find the repo root (.git), writes .claude/settings.json there
mesh model use minimax
mesh model use -g openrouter        # explicit global scope
mesh model use -l openrouter glm-5.1  # local scope with model override
mesh model status                   # show active profile and when it was set
mesh model list                     # show all profiles with descriptions
mesh model check                    # health check active profile
mesh model check minimax            # health check a specific profile

# Start a work session
mesh model use minimax    # or check status
cd ~/environment/pmat/PMATFE-xxx
claude  # Claude Code picks up the active model config

# End a work session — consolidates learnings from ALL repos worked in
mesh dream
mesh dream --dry-run    # preview extracted content without writing
mesh dream --days 5     # process sessions from last 5 days
mesh dream --repo ~/environment/pmat/PMATFE-mfe-profile  # process only specific repo
```

## Token Setup

Tokens are stored in `alpha-mesh/.env` (gitignored). Copy from the example:

```bash
cp .env.example .env
nano .env   # fill in your actual keys
```

Tokens needed for `mesh model` (model switching):
- `ALPHA_MESH_MINIMAX_TOKEN` — your Minimax sk-cp-... key
- `ALPHA_MESH_OPENROUTER_TOKEN` — your OpenRouter SK/... key
- `ALPHA_MESH_ANTHROPIC_TOKEN` — your Anthropic key (optional)

Tokens needed for `mesh dream` (memory consolidation):
- `DREAM_MODEL` — model name (e.g., `MiniMax-M2.7`)
- `DREAM_URL` — API endpoint URL
- `DREAM_TOKEN` — your API token

The `model` and `dream` scripts load `.env` automatically on each run.

## Model Profiles

Edit `config/models.json` to add/change model profiles (non-sensitive config only).
The `model` script writes the active profile to `~/.claude/settings.json`.
Never edit `~/.claude/settings.json` directly anymore.

## Using OpenRouter with different models

```bash
mesh model use openrouter                  # uses openrouter-default profile (glm-5.1), prompts for scope
mesh model use -g openrouter               # explicit global scope
mesh model use -l openrouter glm-5.1       # local scope with model override
mesh model use openrouter tencent/hy3-preview:free   # model override with openrouter-default
mesh model use openrouter deepseek/deepseek-v4-flash  # any OpenRouter model
```

The `openrouter-default` profile provides the OpenRouter URL and token; the model argument overrides the default model. Shorthand `openrouter` resolves to `openrouter-default`. Any model available on OpenRouter can be used.

## Dream (End-of-Session Consolidation)

`mesh dream` scans Claude Code session logs from all repos, extracts learnings, and writes a synthesized `.mesh/MEMORY.md` to each repo.

### How it works

1. **Scan**: Finds `.jsonl` session logs in `~/.claude/projects/*/` modified today
2. **Extract**: Pulls user messages (>20 chars, non-slash-commands) by default. Use `--include-assistant` to also include assistant text (>100 chars)
3. **Group**: Decodes repo paths from the `cwd` field in each session entry
4. **Call LLM**: Uses `DREAM_MODEL`, `DREAM_URL`, `DREAM_TOKEN` from `.env`:
   - Extracts structured learnings (what worked, what failed, patterns, gotchas)
   - Synthesizes with existing `.mesh/MEMORY.md` (deduplicates, merges, caps at 200 lines)
5. **Write**: Output to `{REPO}/.mesh/MEMORY.md` (rewritten each run)

### One-time setup (per repo)

Add this to each repo's `CLAUDE.md` so Claude Code reads the memory file:

```
@.mesh/MEMORY.md
```

Add this to `~/.claude/CLAUDE.md` for cross-repo learnings:

```
@~/.mesh/MEMORY.md
```

Dream does not touch CLAUDE.md — this is a manual step.

### Flags

| Flag | Description |
|------|-------------|
| `--dry-run` | Print extracted content, write nothing |
| `--days N` | Process sessions from last N days |
| `--repo <path>` | Process only specific repo |
| `--include-assistant` | Include assistant messages (default: user only) |

### Error handling

- No sessions today: prints message, exits 0
- LLM unreachable: writes raw extracted content as fallback, exits 0
- Model not configured: exits with error (run `mesh model use` first)

## Review Pull Request Agent

`mesh review-pull-request` runs LLM-powered code review using a fast inference API. It reviews each changed file in two stages (overview + per-file analysis) and posts findings as a PR comment.

### Quick Start

```bash
# Full review — all changed files
mesh review-pull-request https://github.com/org/repo/pull/123

# Incremental review — only changes since the last AI review
mesh review-pull-request https://github.com/org/repo/pull/123 --since-last-review

# Dry run (mocked LLM, no PR comment posted)
mesh review-pull-request https://github.com/org/repo/pull/123 --dry-run
```

### --since-last-review flag

When iterating on a PR, use `--since-last-review` to review only the new commits pushed since the last AI review comment. This is faster and avoids re-flagging issues you already fixed.

| Condition | Behaviour |
|-----------|-----------|
| No previous review found | Falls back to full review |
| No new commits | Exits with `No new commits since last review`. Nothing to review. |

### Documentation

Full documentation: `agents/review-pull-request/README.md`

## Merge Pull Request

`mesh merge-pr <PR url>` is how the owner merges a ticket's PR. It squash-merges only when the PR is open and
its branch already contains the latest commit of its base branch, so nothing lands on a base it was never
tested against (parallel tickets make this likely). It doesn't rely on GitHub branch protection.

```bash
mesh merge-pr https://github.com/org/repo/pull/123
gh pr checks https://github.com/org/repo/pull/123             # the PR's checks (evals-gate among them): merges nothing
```

Every refusal prints the merge status report: a verdict line (READY TO MERGE, or BLOCKED by n items), then one
line per item, `OK` or `BLOCKED`, saying what it found, then the next command:

```
Merge status for PR #65 (head 28348bb): BLOCKED by 1 item

  OK       the PR is open
  OK       it is up to date with master
  OK       it changes 11 agent file(s), so it needs a passing evals status
           agents/review-pull-request/eval/promptfoo/agent_provider.py
           ... (one line per file)
  BLOCKED  evals failed on head 28348bb: runs passed 114/195 · baseline 140/195 · needs at least 130/195 ...

Next: mesh run-evals https://github.com/bbomt/alpha-mesh/pull/65
```

- **Stale:** it refuses and says how many commits the branch is behind. Ask the worker to merge the base in,
  rerun its tests and push, then run it again.
- **The branch moves between the check and the merge:** the merge fails (`--match-head-commit`), so untested
  commits are never merged.
- **A PR that changes the review-pull-request agent** (a file in
  [`list_evals_gated_files.py`](agents/review-pull-request/eval/scripts/list_evals_gated_files.py): its code, prompts, config,
  dependencies, harness or cases): it also needs the commit status `evals` = success on the PR's head commit,
  which `mesh run-evals <PR url>` posts (or on an older commit, if no gated file changed since). Missing, pending or
  failed: it refuses and prints the report with the next command.
  At a terminal, when the evals are all that is missing, it offers to run `mesh run-evals` now and then merge
  (`[y/N]`): the full run is expensive, so it runs once, when the rest of the ticket's code is accepted.
- **GitHub enforces the same rule** for every merge, including its own merge button: the required check
  `evals-gate` ([`.github/workflows/evals-gate.yml`](.github/workflows/evals-gate.yml)) passes at once for a PR that
  changes no agent file, and otherwise only when `evals` = success
  ([the regression gate](agents/review-pull-request/eval/README.md#the-regression-gate)).
- Workers end their "ready to merge" Jira comment with this command, ready to copy.

## Sync Main Checkout

`mesh sync-main --repo-dir <main checkout>` fast-forwards a main checkout to `origin`'s default branch, then
checks that the branch equals `origin`'s and `git status` is clean. Workers and people use it instead of
`git pull` in the main checkout. `--repo-dir` is required, so it never acts on whatever folder you happen to be in.

```bash
mesh sync-main --repo-dir ~/environment/pmat/alpha-mesh
```

- **Run it outside the Claude Code sandbox.** The sandbox blocks writes to `settings.json` files and
  `.git/config`. A fast-forward inside it writes every other file but leaves the branch and index behind. This
  half-applied state looks like dozens of local changes (PMAT-315).
- **Refuses before changing anything** when the checkout is not on its default branch, or is not clean.
- **On failure** it exits non-zero with git's error, lists the files left changed, and prints the repair
  (`git reset --hard origin/<default>`, outside the sandbox, after checking those files only come from the
  incoming commits). It never resets on its own.
- **A 0-byte, read-only `.git/config.lock`** in a main checkout is a sandbox artefact, not a real lock: the
  sandbox mounts `/dev/null` over that path to block config writes, and sometimes leaves the placeholder file
  behind. Delete it outside the sandbox, only when no git process is running.

## mesh orchestrate

`mesh orchestrate --repo-dir <repo folder> PMAT-282 PMAT-283` starts one Claude session per Jira ticket, each in its own tmux tab,
then watches Jira and passes on the owner's comments; after a reboot, `mesh orchestrate` resumes
the workers. Full documentation: `agents/orchestrator/README.md`.
