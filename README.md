# pmat-brain

The memory and orchestration layer for the PMAT agent system.

## Structure

```
pmat-brain/
├── config/                  # Model configs (replaces settings.json swapping)
│   ├── models.json          # All model profiles
│   └── active.json          # Currently active profile (gitignored)
├── scripts/
│   ├── model                # Switch/check model config
│   ├── dream                # End-of-session memory consolidation
│   ├── task-log             # Log a task entry
│   └── bootstrap-memory     # One-time: extract from existing Claude Code logs
├── tasks/
│   └── task-log.db          # SQLite task log (gitignored, backed up separately)
├── memory/
│   ├── sessions/            # Raw dream output per session (YYYY-MM-DD-HHmm.md)
│   ├── learnings/           # Distilled cross-session learnings by topic
│   │   ├── architecture.md
│   │   ├── deployment.md
│   │   ├── testing.md
│   │   └── patterns.md
│   └── skills/              # Extracted workflow skills (reusable prompts/procedures)
│       ├── feature-dev.md
│       └── jira-workflow.md
└── .gitignore
```

## Quick Start

```bash
# Install (run once)
cd ~/environment/pmat
git clone <this-repo> pmat-brain
cd pmat-brain && ./scripts/install

# Switch model
./scripts/model use minimax
./scripts/model use openrouter
./scripts/model status

# Start a work session
./scripts/model use minimax    # or check status
cd ~/environment/pmat/PMATFE-xxx
claude  # Claude Code picks up the active model config

# End a work session
cd ~/environment/pmat/pmat-brain
./scripts/dream
```

## Model Profiles

Edit `config/models.json` to add/change models.
The `model` script writes the active profile to `~/.claude/settings.json`.
Never edit `~/.claude/settings.json` directly anymore.
