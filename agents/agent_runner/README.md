# agent-runner

Generic framework for building Claude + plugin agents.

## What It Does

Wraps Claude Code headlessly with context (JIRA, GitHub, etc.) and a plugin, then handles the output — either posting directly or parsing into structured data.

## Quick Start

```python
from agent_runner import PluginHandler, AgentContext

class MyAgentHandler(PluginHandler):
    @property
    def plugin_name(self) -> str:
        return "my-agent"  # matches /my-agent CLI command

    def build_prompt(self, context: AgentContext) -> str:
        return f"/my-agent medium --comment {context.target}"

    def parse_output(self, output: str, context: AgentContext) -> dict:
        return {"review_text": output, "issues_found": bool(output.strip())}
```

Then in your CLI:

```python
from agent_runner import run_claude_headless

handler = MyAgentHandler()
result = handler.run(context, repo_path="/path/to/repo")
```

## Core Concepts

### PluginHandler

Abstract base class. Subclass it to define:
- `plugin_name` — the slash command (e.g., `"code-review"`)
- `build_prompt(context)` — how to invoke the plugin with context
- `parse_output(output, context)` — how to interpret the result

### AgentContext

Structured context for an agent run:

```python
@dataclass
class AgentContext:
    agent_name: str
    target: str           # e.g., PR URL, file path
    metadata: dict        # free-form key/value
    jira_tickets: list[JiraTicket]
    github_data: Optional[GitHubData]
    extra: dict           # agent-specific extra data
```

### run_claude_headless()

Invokes Claude Code headlessly:

```python
output = run_claude_headless(
    prompt="Your prompt",
    repo_path="/path/to/repo",
    timeout=300,   # seconds
    verbose=True,  # print debug to stderr
)
```

## Output Modes

**Mode A — Plugin posts directly** (no parsing needed):
```python
def parse_output(self, output: str, context: AgentContext) -> dict:
    return {"review_text": output, "issues_found": False, ...}
```

**Mode B — We parse and post ourselves**:
```python
from agent_runner import Review, InlineComment

def parse_output(self, output: str, context: AgentContext) -> dict:
    review = Review(summary="Found issues", inline_comments=[...], verdict="changes_requested")
    return {"review": review, "review_text": output, "issues_found": True, "issue_count": 1}
```

**Mode C — Post raw markdown**:
```python
def parse_output(self, output: str, context: AgentContext) -> dict:
    return {"review": None, "review_text": output, "issues_found": True, "issue_count": 0}
```

## Context Gatherers

Pre-built gatherers for common context sources:

```python
from agent_runner import JiraContextGatherer, GitHubContextGatherer

# Add JIRA ticket details to context
jira_gatherer = JiraContextGatherer()
context = jira_gatherer.gather(context)

# Add GitHub PR data to context
gh_gatherer = GitHubContextGatherer()
context = gh_gatherer.gather(context)
```

## Key Types

| Type | Purpose |
|------|---------|
| `JiraTicket(id, summary, status, priority)` | JIRA ticket data |
| `GitHubData(pr_url, pr_title, author, head_branch, ...)` | GitHub PR data |
| `Review(summary, inline_comments, verdict)` | Structured review output |
| `InlineComment(file, line, severity, comment)` | A single comment on a file/line |

## Adding a New Agent

1. Create `agents/my-agent/` directory
2. Add `agent-runner` as a dependency in `pyproject.toml`:
   ```toml
   dependencies = ["agent-runner @ file:///path/to/agent_runner"]
   ```
3. Create your handler:
   ```python
   from agent_runner import PluginHandler, AgentContext
   class MyAgentHandler(PluginHandler):
       ...
   ```
4. Create a CLI that builds `AgentContext` and calls `handler.run()`

## File Structure

```
agents/agent_runner/
├── agent_runner/
│   ├── __init__.py          # exports all public types
│   ├── agent_runner.py      # PluginHandler, AgentContext, run_claude_headless
│   ├── context_gatherers.py  # JiraContextGatherer, GitHubContextGatherer
│   ├── reviewer.py           # Review, InlineComment, parse_review_output
│   └── types.py              # JiraTicket, GitHubData
└── pyproject.toml
```