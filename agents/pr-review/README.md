# pr-review - AI-Powered Code Review Agent

The `pr-review` agent automates code reviews by running Claude Code locally inside the target repository. This gives full codebase context (not just the diff), eliminating false positives common with isolated reviews.

## Prerequisites

1. **Claude Code CLI** - Must be installed and authenticated
2. **GitHub CLI (`gh`)** - For GitHub API authentication
   ```bash
   gh auth login
   ```
3. **Atlassian MCP Plugin** - For JIRA ticket lookups (optional)
   - Run `claude` in your terminal
   - Activate the Atlassian plugin
   - Complete the OAuth flow

## Installation

The `pr-review` agent is part of the `alpha-mesh` monorepo. No separate installation needed if you already have `alpha-mesh` set up.

Ensure `alpha-mesh/bin/` is on your PATH:
```bash
# Add to ~/.bashrc or ~/.zshrc
export PATH="/path/to/alpha-mesh/bin:$PATH"
```

## Usage

### Basic Review

```bash
mesh pr-review https://github.com/org/repo/pull/123
```

This will:
1. Fetch PR metadata, diff, and changed files from GitHub
2. Extract any JIRA ticket IDs from the PR title/branch/description
3. Build a review prompt with full codebase context
4. Run Claude Code headlessly to perform the review
5. Post a summary comment and inline comments to the GitHub PR

### Auto-Fix Mode

```bash
mesh pr-review https://github.com/org/repo/pull/123 --auto-fix
```

In addition to the review, this will:
- Allow Claude Code to make changes to fix issues
- Commit the changes with message: `chore: apply AI code review suggestions [pr-review]`
- Push to the PR branch

### Other Flags

| Flag | Description |
|------|-------------|
| `--repo-path PATH` | Local path to the repository (default: current working directory) |
| `--verbose` | Print detailed logs to stderr |
| `--dry-run` | Run everything except posting the comment and pushing |
| `--timeout N` | Timeout in seconds for Claude Code (default: 300) |

## Examples

```bash
# Review a PR (from any directory)
mesh pr-review https://github.com/facebook/react/pull/12345

# Review with verbose output
mesh pr-review https://github.com/org/repo/pull/456 --verbose

# Dry run - see what would be posted without actually posting
mesh pr-review https://github.com/org/repo/pull/789 --dry-run --verbose

# Auto-fix mode - apply fixes and push
cd ~/environment/pmat/PMATFE-mfe-profile
mesh pr-review https://github.com/org/repo/pull/123 --auto-fix
```

## Exit Codes

| Code | Meaning |
|------|----------|
| `0` | Review completed successfully |
| `1` | General error (network, config, etc.) |
| `2` | GitHub API error |
| `3` | Atlassian MCP auth not available in Claude Code |
| `4` | Claude Code subprocess failed |
| `5` | Git error (wrong branch, dirty state, push failure) |

## How It Works

1. **Parse PR URL** - Extracts owner, repo, and PR number
2. **Fetch from GitHub** - Gets PR metadata, diff, and changed files
3. **Extract JIRA IDs** - Scans PR title, branch name, and description
4. **Verify Atlassian Auth** - Checks if Atlassian MCP plugin is authenticated (if JIRA IDs found)
5. **Checkout PR Branch** - Switches to the PR branch in the local repo
6. **Build Context Prompt** - Assembles PR info, JIRA context, and diff
7. **Run Claude Code** - Headless review with full repo access
8. **Parse Review Output** - Extracts JSON review from Claude Code output
9. **Post to GitHub** - Creates PR review with summary and inline comments
10. **Auto-fix** (optional) - Commit and push fixes

## Configuration

The agent uses:
- **GitHub Auth** - Via `gh auth token` (GitHub CLI)
- **Claude Code Auth** - Existing Claude Code authentication
- **Atlassian Auth** - Via Claude Code's Atlassian MCP plugin

No tokens or credentials are stored by the agent itself.

## Testing

```bash
cd agents/pr-review
uv run pytest -v
```

Current test coverage: 33 tests
- `test_jira_client.py` - JIRA ID extraction
- `test_reviewer.py` - Review output parsing
- `test_context_builder.py` - Prompt building

## Limitations

1. **Inline comment line numbers** - Claude Code returns file-absolute line numbers; GitHub API may expect diff positions
2. **Atlassian MCP auth check** - Currently heuristic-based
3. **No retry logic** - For GitHub API rate limits or transient failures

## Next Steps

See `NOTES.md` for implementation details and known limitations.
