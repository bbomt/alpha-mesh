"""Context gatherers for agent runner framework."""

from abc import ABC, abstractmethod
from typing import Optional
import subprocess
import sys

from agent_runner.types import JiraTicket, GitHubData
from agent_runner.agent_runner import AgentContext, ClaudeInvocationError, run_claude_headless, log_agent_run


class ContextGatherer(ABC):
    """Abstract base class for context gatherers."""

    @abstractmethod
    def gather(self, agent_context: AgentContext) -> AgentContext:
        """Gather context and return updated AgentContext."""
        pass


class JiraContextGatherer(ContextGatherer):
    """Gathers JIRA ticket context using Atlassian MCP."""

    def gather(self, agent_context: AgentContext) -> AgentContext:
        """Look up JIRA tickets mentioned in the context."""
        if not agent_context.jira_tickets:
            # Extract JIRA IDs from metadata if present
            jira_ids = agent_context.metadata.get("jira_ids", [])
            if jira_ids:
                # Fetch ticket details using Atlassian MCP via Claude
                tickets = self._fetch_jira_tickets(jira_ids, agent_context.metadata.get("repo_path", ""))
                agent_context.jira_tickets = tickets

        return agent_context

    def _fetch_jira_tickets(self, jira_ids: list[str], repo_path: str) -> list[JiraTicket]:
        """Fetch JIRA ticket details using Claude Code headless with Atlassian MCP."""
        if not jira_ids:
            return []

        # Build a prompt to ask Claude to look up the tickets via Atlassian MCP
        jira_list = ", ".join(jira_ids)
        prompt = f"""Please look up the following JIRA tickets using your Atlassian MCP plugin and return details for each:

{jira_list}

For each ticket, return:
- ID (e.g., PMAT-93)
- Summary (short description)
- Status
- Priority

Format your response as:
[
  {{"id": "PMAT-93", "summary": "...", "status": "...", "priority": "..."}},
  ...
]
"""

        try:
            output = run_claude_headless(prompt, repo_path, timeout=60, verbose=False)
            log_agent_run("jira_lookup", prompt, output)

            # Parse JSON response
            import json
            import re

            # Try to extract JSON from output (non-greedy to avoid spanning multiple arrays)
            json_match = re.search(r'\[.*?\]', output, re.DOTALL)
            if json_match:
                tickets_data = json.loads(json_match.group(0))
                tickets = []
                for t in tickets_data:
                    tickets.append(JiraTicket(
                        id=t.get("id", ""),
                        summary=t.get("summary", ""),
                        status=t.get("status", ""),
                        priority=t.get("priority", ""),
                        description=t.get("description", ""),
                    ))
                return tickets

        except (json.JSONDecodeError, ClaudeInvocationError) as e:
            print(f"Warning: Failed to fetch JIRA tickets: {e}", file=sys.stderr)

        return []


class GitHubContextGatherer(ContextGatherer):
    """Gathers GitHub PR context."""

    def gather(self, agent_context: AgentContext) -> AgentContext:
        """Fetch GitHub PR data if target is a PR URL."""
        if not agent_context.github_data:
            pr_url = agent_context.target
            if "github.com" in pr_url and "/pull/" in pr_url:
                github_data = self._fetch_pr_data(pr_url)
                if github_data:
                    agent_context.github_data = github_data

        return agent_context

    def _fetch_pr_data(self, pr_url: str) -> Optional[GitHubData]:
        """Fetch PR data from GitHub using gh CLI."""
        import re

        # Parse PR URL
        match = re.match(r'https://github\.com/([^/]+)/([^/]+)/pull/(\d+)', pr_url)
        if not match:
            return None

        owner, repo, pr_number = match.groups()

        try:
            # Get PR details via gh
            result = subprocess.run(
                ["gh", "pr", "view", str(pr_number), "--json", "title,body,author,headRefName,baseRefName,state"],
                capture_output=True,
                text=True,
                timeout=30,
            )

            if result.returncode != 0:
                return None

            import json
            pr_data = json.loads(result.stdout)

            # Get diff
            diff_result = subprocess.run(
                ["gh", "pr", "diff", str(pr_number)],
                capture_output=True,
                text=True,
                timeout=30,
            )
            diff = diff_result.stdout if diff_result.returncode == 0 else ""

            # Get changed files
            files_result = subprocess.run(
                ["gh", "api", f"repos/{owner}/{repo}/pulls/{pr_number}/files"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            changed_files = []
            if files_result.returncode == 0:
                files_data = json.loads(files_result.stdout)
                changed_files = [f.get("filename", "") for f in files_data]

            return GitHubData(
                pr_url=pr_url,
                pr_title=pr_data.get("title", ""),
                pr_body=pr_data.get("body", "") or "",
                author=pr_data.get("author", {}).get("login", ""),
                head_branch=pr_data.get("headRefName", ""),
                base_branch=pr_data.get("baseRefName", ""),
                state=pr_data.get("state", ""),
                diff=diff,
                changed_files=changed_files,
            )

        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
            print(f"Warning: Failed to fetch GitHub PR data: {e}", file=sys.stderr)
            return None