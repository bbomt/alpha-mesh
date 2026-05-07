# Skill: model-routing

## What it does
Selects the right LLM model/endpoint for each task type, balancing cost and quality.

## Model profiles

| Profile | Endpoint | Best for | Cost |
|---|---|---|---|
| `minimax` | api.minimax.io direct | Code generation, feature dev, most tasks | Very low (5hr token window) |
| `openrouter-glm` | openrouter.ai | Code review, architecture discussion | Medium |
| `claude-sonnet` | api.anthropic.com | Strategic synthesis, complex reasoning | High (use sparingly) |

## Decision rules
1. **Default**: always start with `minimax`
2. **Code review / PR review**: switch to `openrouter-glm`
3. **Architecture decision with high ambiguity**: `claude-sonnet`
4. **Model unavailable**: `model use <next>` → check `fallback_order` in models.json
5. **End of 5hr Minimax window**: switch to `openrouter-glm` for remainder of session

## Commands
```bash
model status              # what's active
model use minimax         # switch
model check openrouter-glm  # health check without switching
model list                # all profiles
```

## Adding a new model
1. Edit `config/models.json` — add entry under `profiles`
2. Test: `model use <new-profile>`
3. Document here under Model profiles table

## Cost tracking (manual for now)
Log model used per task: `task-log start` captures the active model automatically.
Review with: `task-log recent 20` to see model distribution over time.
