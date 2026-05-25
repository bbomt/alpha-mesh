"""Generic agent runner for Claude + plugin agents."""

import shutil
import subprocess
import os
import tempfile
import time
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Any
from abc import ABC, abstractmethod

from agent_runner.types import JiraTicket, GitHubData


LOG_DIR = Path.home() / "environment" / "logs" / "ai" / "claude"
LOG_FILE = LOG_DIR / f"alpha-mesh-{time.strftime('%Y-%m-%d')}.log"


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
    claude_path = shutil.which("claude") or "/home/ec2-user/.local/bin/claude"
    cmd = [claude_path, "--print"]

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
        raise ClaudeInvocationError(
            "Claude CLI not found. Ensure 'claude' is installed and on your PATH, "
            "or that /home/ec2-user/.local/bin/claude exists."
        )


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
    except Exception as e:
        print(f"Warning: failed to write agent run log: {e}", file=sys.stderr)


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