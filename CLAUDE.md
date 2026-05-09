# CLAUDE.md

@../architecture/CLAUDE_GLOBAL.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

alpha-mesh is the memory and orchestration layer for the PMAT agent system. It provides a `mesh` CLI for model routing, session memory consolidation, and task logging.

## Architecture

```
alpha-mesh/
├── bin/                     # Executable bash scripts; mesh is the unified dispatcher
│   ├── mesh                 # Entry point: mesh <command>
│   ├── model                # Model switching (local/global scope)
│   ├── dream                # End-of-session memory consolidation
│   ├── task-log             # SQLite-backed task log
│   ├── bootstrap-memory     # Extract memory from existing Claude Code logs
│   ├── lib/                  # Shared library
│   │   └── repo              # find_repo_root, get_active_model_from_settings
│   └── install              # One-time setup (run after clone)
├── test/
│   ├── run-all              # Runs all test suites
│   └── test-*               # Individual test suites
├── config/
│   └── models.json          # Model profiles (provider, URL, default model)
└── memory/
    ├── sessions/            # Raw dream output per session (YYYY-MM-DD-HHmm.md)
    ├── learnings/           # Cross-session learnings by topic
    └── skills/              # Extracted reusable workflow prompts
```

## Commands

```bash
# Install (run once)
./bin/install

# Model management
mesh model use minimax           # switch model, prompt for scope
mesh model use -g openrouter    # explicit global scope
mesh model use -l openrouter glm-5.1  # local scope + model override
mesh model status               # show active model config
mesh model list                 # list all profiles
mesh model check [profile]      # health check without switching

# Task logging
mesh task-log start PMAT-NNN "title" --repo PMATFE-xxx
mesh task-log done PMAT-NNN --outcome "what shipped"
mesh task-log show PMAT-NNN

# Memory
mesh dream                      # consolidate session into memory/
mesh bootstrap-memory          # seed memory from existing Claude Code logs
mesh bootstrap-memory --dry-run

# Validation
mesh test-alpha-mesh  # broken into test/test/run-all
```

## Model Configuration

- Tokens: stored in `.env` (gitignored), not in `models.json`
- `config/models.json` defines profiles (provider, base URL, default model)
- Local scope writes to `<repo-root>/.claude/settings.json` (takes precedence)
- Global scope writes to `~/.claude/settings.json`
- Local scope walks up from CWD to find the nearest `.git` directory

## Dependencies

`jq`, `sqlite3`, `curl`, `python3` — required by the install script.

## Key Design Notes

- Scripts are pure bash — no build step, no package manager
- `bin/mesh` dispatches to sub-scripts; call individual scripts directly or via `mesh`
- `task-log` uses SQLite at `tasks/task-log.db` (gitignored)
- `dream` output goes to `memory/sessions/`; learnings are appended to `memory/learnings/<topic>.md`
