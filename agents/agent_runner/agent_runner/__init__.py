"""agent_runner - Generic framework for Claude + plugin agents.

This package provides:
- PluginHandler ABC: base class for plugin-based agents
- AgentContext: structured context for agent runs
- run_claude_headless(): invoke Claude Code headlessly
- Context gatherers: JiraContextGatherer, GitHubContextGatherer
- Review types: Review, InlineComment

Usage:
    from agent_runner import PluginHandler, AgentContext, run_claude_headless

    class MyAgentHandler(PluginHandler):
        @property
        def plugin_name(self) -> str:
            return "my-agent"

        def build_prompt(self, context: AgentContext) -> str:
            return f"/my-agent medium"

        def parse_output(self, output: str, context: AgentContext) -> dict:
            return {"review_text": output, "issues_found": bool(output.strip())}
"""

from agent_runner.types import JiraTicket, GitHubData
from agent_runner.agent_runner import (
    AgentContext,
    ClaudeInvocationError,
    PluginHandler,
    run_claude_headless,
    log_agent_run,
)
from agent_runner.reviewer import Review, InlineComment, ReviewParseError, parse_review_output
from agent_runner.context_gatherers import ContextGatherer, JiraContextGatherer, GitHubContextGatherer

__all__ = [
    # types
    "JiraTicket",
    "GitHubData",
    # core
    "AgentContext",
    "ClaudeInvocationError",
    "PluginHandler",
    "run_claude_headless",
    "log_agent_run",
    # reviewer
    "Review",
    "InlineComment",
    "ReviewParseError",
    "parse_review_output",
    # context gatherers
    "ContextGatherer",
    "JiraContextGatherer",
    "GitHubContextGatherer",
]