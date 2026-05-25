# pr-review-plugin - AI-Powered Code Review Agent (Plugin-based)

The `pr-review-plugin` agent runs the `/code-review` plugin headlessly to review PRs and post comments directly to GitHub. Full codebase context is available since Claude Code runs inside the target repository.

## Prerequisites

1. **Claude Code CLI** - Must be installed and authenticated
2. **GitHub CLI (`gh`)** - For GitHub API authentication

   Install on Amazon Linux 2023:
   ```bash
   sudo dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo
   sudo dnf install gh -y
   ```

   Authenticate:
   ```bash
   gh auth login
   ```

## Installation

The `pr-review` agent is part of the `alpha-mesh` monorepo. No separate installation needed if you already have `alpha-mesh` set up.

## Usage

```bash
mesh pr-review-plugin https://github.com/org/repo/pull/123
```

### Flags

| Flag | Description |
|------|-------------|
| `--repo-path PATH` | Local path to the repository (default: current working directory) |
| `--verbose` | Print detailed logs to stderr |
| `--dry-run` | Run everything except posting the comment |
| `--timeout N` | Timeout in seconds for Claude Code (default: 2400) |
| `--hide-pr-comment` | Suppress printing the posted PR comment to stdout |

## Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Review completed successfully |
| `1` | Invalid PR URL or repository path |
| `2` | GitHub API error |
| `4` | Claude Code subprocess failed |
| `5` | Git error (wrong branch, dirty state) |

## How It Works

1. **Parse PR URL** - Extracts owner, repo, and PR number from the GitHub URL
2. **Fetch PR data** - Gets PR title, body, author, branch, and base branch from GitHub
3. **Checkout PR branch** - Switches to the PR branch in the local repo
4. **Build context** - Assembles PR info into an AgentContext
5. **Run /code-review plugin** - Invokes Claude Code headlessly with the plugin
6. **Post to GitHub** - Posts the raw plugin output as a PR issue comment

## Testing

```bash
cd agents/pr-review
uv run pytest -v
```