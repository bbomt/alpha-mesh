"""Shared types for agent runner framework."""

from dataclasses import dataclass, field


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