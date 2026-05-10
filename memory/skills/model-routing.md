# Skill: model-routing

## What it does
Selects the right LLM model/endpoint for each task type, balancing cost and quality.

## Model profiles

| Profile | Endpoint | Best for | Cost |
|---|---|---|---|
| `minimax` | api.minimax.io direct | Code generation, feature dev, most tasks | Very low (5hr token window) |
| `openrouter-default` | openrouter.ai | Base profile — override model as needed | Per-model |
| `claude-sonnet` | api.anthropic.com | Strategic synthesis, complex reasoning | High (use sparingly) |

## Decision rules
1. **Default**: always start with `minimax`
2. **Code review / PR review**: `model use openrouter <model>` (e.g. `glm-5.1`)
3. **Architecture decision with high ambiguity**: `model use claude-sonnet`
4. **Model unavailable**: `model use <next>` → check `fallback_order` in models.json
5. **End of 5hr Minimax window**: `model use openrouter` for remainder of session

## Commands
```bash
model status                    # what's active
model use minimax               # switch to minimax
model use openrouter            # switch to openrouter-default
model use openrouter glm-5.1    # switch to openrouter with specific model
model use openrouter deepseek/deepseek-v4-flash  # any openrouter model
model check openrouter-default  # health check without switching
model list                      # all profiles
```

## Adding a new model
1. Edit `config/models.json` — add entry under `profiles`
2. Test: `model use <new-profile>`
3. Document here under Model profiles table

## Cost tracking (manual for now)
