# alpha-mesh

The memory and orchestration layer for the PMAT agent system.

## Structure

```
alpha-mesh/
├── bin/                     # Executable scripts (symlinked to ~/bin)
│   ├── model                # Switch/check model config
│   ├── dream                # End-of-session memory consolidation
│   ├── bootstrap-memory     # One-time: extract from existing Claude Code logs
│   ├── test/                 # Test suites (run via test/run-all)
│   └── mesh                 # Unified CLI entry point (wraps all commands)
├── config/                  # Model configs
│   └── models.json          # All model profiles
├── memory/
│   ├── sessions/            # Raw dream output per session (YYYY-MM-DD-HHmm.md)
│   ├── learnings/           # Distilled cross-session learnings by topic
│   │   ├── architecture.md
│   │   ├── deployment.md
│   │   ├── testing.md
│   │   ├── models.md
│   │   ├── workflow.md
│   │   └── patterns.md
│   └── skills/              # Extracted workflow skills (reusable prompts/procedures)
│       ├── feature-dev.md
│       └── model-routing.md
└── .gitignore
```

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

# End a work session
cd ~/environment/pmat/alpha-mesh
mesh dream
```

## Token Setup

Tokens are stored in `alpha-mesh/.env` (gitignored). Copy from the example:

```bash
cp .env.example .env
nano .env   # fill in your actual keys
```

Tokens needed:
- `ALPHA_MESH_MINIMAX_TOKEN` — your Minimax sk-cp-... key
- `ALPHA_MESH_OPENROUTER_TOKEN` — your OpenRouter SK/... key
- `ALPHA_MESH_ANTHROPIC_TOKEN` — your Anthropic key (optional)

The `model` script loads `.env` automatically on each run. You can also set these as environment variables in your shell profile.

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
