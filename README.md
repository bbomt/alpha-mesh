# alpha-mesh

The memory and orchestration layer for the PMAT agent system.

## Structure

```
alpha-mesh/
├── bin/                     # Executable scripts (symlinked to ~/bin)
│   ├── model                # Switch/check model config
│   ├── dream                # End-of-session memory consolidation (dream)
│   ├── bootstrap-memory     # One-time: extract from existing Claude Code logs
│   ├── lib/
│   │   └── repo             # Shared functions: find_repo_root, get_active_model
│   ├── test/                # Test suites (run via test/run-all)
│   ├── install              # One-time setup
│   └── mesh                 # Unified CLI entry point (wraps all commands)
├── config/
│   └── models.json          # All model profiles
├── memory/
│   ├── sessions/            # Historical dream output (legacy — no longer written)
│   ├── learnings/           # Historical learnings (legacy — no longer written)
│   └── skills/              # Extracted workflow skills (reusable prompts/procedures)
├── test/
│   ├── run-all              # Runs all test suites
│   ├── test-dream           # Tests for bin/dream
│   ├── test-model           # Tests for bin/model
│   └── test-install         # Tests for bin/install
└── .gitignore
```

Dream output goes to `{REPO}/.mesh/MEMORY.md` in each repo worked in.
Cross-repo learnings go to `~/.mesh/MEMORY.md`.

## Quick Start

```bash
# Install (run once)
cd ~/environment/pmat
git clone <this-repo> alpha-mesh
cd alpha-mesh && ./bin/install

# The `mesh` CLI is now available globally — prefix for all commands:
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
2. **Extract**: Pulls assistant text (>100 chars), user messages (>20 chars, non-slash-commands), and error/fail lines
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
| `--5-days` | Process sessions from last 5 days |
| `--days N` | Process sessions from last N days |

### Error handling

- No sessions today: prints message, exits 0
- LLM unreachable: writes raw extracted content as fallback, exits 0
- Model not configured: exits with error (run `mesh model use` first)
