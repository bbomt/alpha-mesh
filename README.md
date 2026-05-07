# alpha-mesh

The memory and orchestration layer for the PMAT agent system.

## Structure

```
alpha-mesh/
├── config/                  # Model configs (replaces settings.json swapping)
│   ├── models.json          # All model profiles
│   └── active.json          # Currently active profile (gitignored)
├── scripts/
│   ├── model                # Switch/check model config
│   ├── dream                # End-of-session memory consolidation
│   ├── task-log             # Log a task entry
│   ├── bootstrap-memory     # One-time: extract from existing Claude Code logs
│   └── test-brain           # Validation test
├── tasks/
│   └── task-log.db          # SQLite task log (gitignored, backed up separately)
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
cd alpha-mesh && ./scripts/install

# Switch model
./scripts/model use minimax
./scripts/model use openrouter
./scripts/model status

# Start a work session
./scripts/model use minimax    # or check status
cd ~/environment/pmat/PMATFE-xxx
claude  # Claude Code picks up the active model config

# End a work session
cd ~/environment/pmat/alpha-mesh
./scripts/dream
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
model use openrouter                  # uses openrouter-default profile (glm-5.1)
model use openrouter tencent/hy3-preview:free
model use openrouter deepseek/deepseek-v4-flash
```

The `openrouter-default` profile provides the OpenRouter URL and token; the model argument overrides the default model. Shorthand `openrouter` resolves to `openrouter-default`. Any model available on OpenRouter can be used.
