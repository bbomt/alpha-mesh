"""Generic agent runner for Claude + plugin agents."""

import subprocess
import os
import json
import tempfile
import time
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Any
from abc import ABC, abstractmethod


LOG_DIR = Path.home() / "environment" / "logs" / "ai" / "claude"
LOG_FILE = LOG_DIR / f"alpha-mesh-{time.strftime('%Y-%m-%d')}.log"


@dataclass
class JiraTicket:
    """Represents a JIRA ticket."""
    id: str
    summary: str
    status: str
    priority: str
    description: str = ""


@dataclass
class GitHubData:
    """Represents GitHub PR data."""
    pr_url: str
    pr_title: str
    pr_body: str
    author: str
    head_branch: str
    base_branch: str
    state: str
    diff: str = ""
    changed_files: list[str] = field(default_factory=list)


@dataclass
class AgentContext:
    """Structured context for an agent run."""
    agent_name: str
    target: str  # e.g., PR URL, file path, etc.
    metadata: dict[str, Any] = field(default_factory=dict)
    jira_tickets: list[JiraTicket] = field(default_factory=list)
    github_data: Optional[GitHubData] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_text(self) -> str:
        """Convert context to human-readable text for plugin prompts."""
        parts = [f"Agent: {self.agent_name}"]
        parts.append(f"Target: {self.target}")

        if self.metadata:
            meta_str = ", ".join(f"{k}={v}" for k, v in self.metadata.items())
            parts.append(f"Metadata: {meta_str}")

        if self.jira_tickets:
            jira_ids = ", ".join(t.id for t in self.jira_tickets)
            parts.append(f"\nJIRA Tickets ({len(self.jira_tickets)}): {jira_ids}")
            for ticket in self.jira_tickets:
                parts.append(f"  - [{ticket.id}] {ticket.summary} ({ticket.status})")

        if self.github_data:
            gh = self.github_data
            parts.append(f"\nGitHub PR: {gh.pr_title}")
            parts.append(f"Author: {gh.author}")
            parts.append(f"Branch: {gh.head_branch} -> {gh.base_branch}")
            parts.append(f"State: {gh.state}")
            if gh.changed_files:
                parts.append(f"Changed files: {len(gh.changed_files)}")

        return "\n".join(parts)


class ClaudeInvocationError(Exception):
    """Raised when Claude headless invocation fails."""
    pass


def run_claude_headless(
    prompt: str,
    repo_path: str,
    timeout: int = 300,
    verbose: bool = False,
) -> str:
    """Run Claude Code headlessly with a prompt.

    Args:
        prompt: The prompt to send to Claude
        repo_path: Path to run Claude in
        timeout: Timeout in seconds
        verbose: Print debug info to stderr

    Returns:
        Raw stdout from Claude

    Raises:
        ClaudeInvocationError on failure
    """
    cmd = ["/home/ec2-user/.local/bin/claude", "--print"]

    if verbose:
        print(f"Running Claude: {' '.join(cmd)}", file=sys.stderr)
        print(f"CWD: {repo_path}", file=sys.stderr)
        print(f"Prompt length: {len(prompt)} chars", file=sys.stderr)

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            with open(prompt_file) as pf:
                result = subprocess.run(
                    cmd,
                    cwd=repo_path,
                    stdin=pf,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
        finally:
            os.unlink(prompt_file)

        if verbose:
            if result.stderr:
                print(result.stderr, file=sys.stderr)
            print(f"Exit code: {result.returncode}", file=sys.stderr)

        if result.returncode != 0:
            error_msg = f"Claude exited with code {result.returncode}"
            if result.stderr:
                error_msg += f": {result.stderr}"
            raise ClaudeInvocationError(error_msg)

        return result.stdout

    except subprocess.TimeoutExpired:
        raise ClaudeInvocationError(f"Claude timed out after {timeout}s")
    except FileNotFoundError:
        raise ClaudeInvocationError("Claude CLI not found at /home/ec2-user/.local/bin/claude")


def log_agent_run(agent_name: str, prompt: str, output: str = "", error: str = "") -> None:
    """Log agent run to file."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"\n## Agent: {agent_name} @ {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"PROMPT:\n{prompt[:500]}...\n")
            if output:
                f.write(f"OUTPUT:\n{output[:1000]}...\n")
            if error:
                f.write(f"ERROR:\n{error}\n")
    except Exception:
        pass


class PluginHandler(ABC):
    """Abstract base class for plugin handlers."""

    @property
    @abstractmethod
    def plugin_name(self) -> str:
        """Name of the plugin (e.g., 'code-review')."""
        pass

    @abstractmethod
    def build_prompt(self, context: AgentContext) -> str:
        """Build the prompt to send to Claude with the plugin invocation."""
        pass

    @abstractmethod
    def parse_output(self, output: str, context: AgentContext) -> dict[str, Any]:
        """Parse the raw output from Claude into structured data."""
        pass

    def run(self, context: AgentContext, repo_path: str, timeout: int = 600, verbose: bool = False) -> dict[str, Any]:
        """Run the plugin with the given context.

        Args:
            context: Structured agent context
            repo_path: Path to run Claude in
            timeout: Timeout in seconds
            verbose: Print debug info

        Returns:
            Parsed output dict from the plugin
        """
        prompt = self.build_prompt(context)

        if verbose:
            print(f"Plugin: {self.plugin_name}", file=sys.stderr)
            print(f"Prompt preview: {prompt[:200]}...", file=sys.stderr)

        raw_output = run_claude_headless(prompt, repo_path, timeout, verbose)
        log_agent_run(self.plugin_name, prompt, raw_output)

        return self.parse_output(raw_output, context)


class CodeReviewPluginHandler(PluginHandler):
    """Handler for /code-review plugin."""

    @property
    def plugin_name(self) -> str:
        return "code-review"

    def build_prompt(self, context: AgentContext) -> str:
        """Build prompt for /code-review with JIRA context."""
        parts = ["/code-review medium --comment"]

        # Append JIRA context if available
        if context.jira_tickets:
            jira_ids = ", ".join(t.id for t in context.jira_tickets)
            parts.append(f"\n\nLinked JIRA tickets: {jira_ids}")
            parts.append("Please review with context from these tickets:\n")
            for ticket in context.jira_tickets:
                parts.append(f"  - [{ticket.id}] {ticket.summary}")

        # Append GitHub PR info if available
        if context.github_data:
            gh = context.github_data
            parts.append(f"\n\nPR: {gh.pr_title}")
            parts.append(f"Author: {gh.author}")
            parts.append(f"Branch: {gh.head_branch}")

        return "".join(parts)

    def parse_output(self, output: str, context: AgentContext) -> dict[str, Any]:
        """Parse /code-review plugin output.

        Returns dict with:
            - review: Review object (or None if no issues)
            - review_text: the raw text output
            - issues_found: bool
            - issue_count: int
        """
        import json
        import re

        review_text = output.strip()
        issues_found = False
        issue_count = 0
        review = None

        # Try to extract JSON from ```json ... ``` block
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', output, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                inline_comments = data.get("inline_comments", [])
                if isinstance(inline_comments, list) and len(inline_comments) > 0:
                    issues_found = True
                    issue_count = len(inline_comments)

                    from pr_review.reviewer import InlineComment
                    comments = []
                    for item in inline_comments:
                        if isinstance(item, dict) and "file" in item and "line" in item:
                            severity = item.get("severity", "suggestion")
                            if severity not in ("error", "warning", "suggestion"):
                                severity = "suggestion"
                            comments.append(InlineComment(
                                file=item["file"],
                                line=int(item["line"]),
                                severity=severity,
                                comment=item.get("comment", ""),
                            ))

                    from pr_review.reviewer import Review
                    review = Review(
                        summary=data.get("summary", "Code review complete"),
                        inline_comments=comments,
                        verdict=data.get("verdict", "comment"),
                    )
            except (json.JSONDecodeError, ValueError, KeyError):
                pass

        # Handle text-based markdown findings (e.g., "### Finding 1 — ...")
        if not issues_found:
            finding_pattern = r'### Finding (\d+)'
            findings = re.findall(finding_pattern, output)
            if findings:
                issues_found = True
                issue_count = len(findings)

                # Extract text-based findings into a summary
                from pr_review.reviewer import Review
                summary = f"Code review found {issue_count} issue(s). See inline comments for details."

                # Try to extract severity from output
                severity = "suggestion"
                if "Severity:" in output or "**Severity:**" in output:
                    severity_match = re.search(r'(?:Severity:|\*\*Severity:\*\*)\s*(?:Medium|High|Critical|Low|Low)', output, re.IGNORECASE)
                    if severity_match:
                        sev_text = severity_match.group(1).lower()
                        if sev_text in ("high", "critical"):
                            severity = "error"
                        elif sev_text == "medium":
                            severity = "warning"
                        else:
                            severity = "suggestion"

                # Build a single summary inline comment with the key findings
                finding_lines = []
                for match in re.finditer(r'### Finding (\d+) — (.+?)(?:\n|$)', output):
                    finding_num = match.group(1)
                    finding_title = match.group(2).strip()
                    finding_lines.append(f"Finding {finding_num}: {finding_title}")

                if finding_lines:
                    summary = "; ".join(finding_lines)

                review = Review(
                    summary=summary,
                    inline_comments=[],
                    verdict="changes_requested" if severity in ("error", "warning") else "comment",
                )

        # Fallback: check for text-based "Found X issue" pattern
        if not issues_found:
            match = re.search(r'Found (\d+) issue', output, re.IGNORECASE)
            if match:
                issues_found = True
                issue_count = int(match.group(1))

        return {
            "review": review,
            "review_text": review_text,
            "issues_found": issues_found,
            "issue_count": issue_count,
        }