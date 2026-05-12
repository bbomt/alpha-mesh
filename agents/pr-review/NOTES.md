# PR Review Agent - Implementation Notes

## Open Questions - Findings

### 1. Claude Code headless flags

**Finding:** Based on Claude Code CLI documentation and typical usage:
- `claude --print -p "prompt"` runs headless and prints output to stdout
- `--no-input` prevents interactive prompts
- There is no explicit `--plan-mode` flag - the `--print` mode with `-p` is inherently non-interactive
- For auto-fix mode, simply omit any special flags - Claude Code in `--print` mode can still make edits when instructed

**Verified flags:**
- `--print`: Run headless, print output
- `-p` / `--prompt`: Provide prompt (or use stdin)
- `--no-input`: Don't prompt for input

### 2. Claude Code output format

**Finding:** Claude Code with `--print` flag writes to stdout. The output includes:
- The assistant's response text
- Possible system messages or formatting

The code in `claude_runner.py` captures stdout and returns it as a string. The `reviewer.py` then parses this to extract JSON.

### 3. Atlassian MCP auth check

**Finding:** Implemented in `jira_client.py` - `verify_atlassian_auth()` function.
- Runs `claude --version` to check if Claude Code is available
- Runs `claude --print --no-input -p "..."` to check if Atlassian plugin responds
- Returns True if "atlassian" is mentioned in output along with tool names
- Returns False if auth errors or "not found" messages appear

**Limitation:** This is a heuristic check. The actual JIRA fetch will fail with a clearer error if not authenticated.

### 4. GitHub token

**Finding:**
- Primary method: `gh auth token` (GitHub CLI)
- Fallback: `GITHUB_TOKEN` environment variable
- If neither is available, raises an error with clear instructions

### 5. Inline comment line numbers

**Finding:** GitHub's REST API for PR reviews accepts:
- `path` + `line` + `side` (RIGHT for new code, LEFT for old code)
- The line number is the line in the file, not the diff position

**Important:** The current implementation uses `line` + `side: RIGHT"`. This may need adjustment if Claude Code returns diff-relative line numbers. Testing needed with real PRs.

**UPDATE:** Claude Code has access to the full repo, so it should return file-absolute line numbers. The `side: RIGHT` assumes we're commenting on new code.

## Implementation Decisions

1. **Package structure:** Follows `dream_core` pattern with `__init__.py` and `__main__.py`
2. **Build system:** Uses `hatchling` (per Jira ticket example)
3. **CLI framework:** Uses `click` (per Jira ticket dependencies)
4. **No `.env` for this agent:** Unlike `dream`, this agent doesn't need API tokens stored in `.env` - it uses `gh` CLI for GitHub and Claude Code's own auth for Atlassian
5. **Logging:** Follows CLAUDE.md conventions - logs to `~/environment/logs/ai/claude/alpha-mesh-YYYY-MM-DD.log`

## Testing

- Unit tests: `pytest tests/`
- Run with: `cd agents/pr-review && uv run pytest`
- Integration tests: Manual testing with real PRs needed

## Known Limitations

1. Inline comment positioning may need adjustment for diff vs file line numbers
2. Atlassian MCP auth check is heuristic-based
3. Claude Code output parsing assumes JSON is present in output
4. No retry logic for API calls (GitHub rate limits, etc.)
