"""Code review plugin handler for pr-review agent."""

from agent_runner import AgentContext
from agent_runner.agent_runner import PluginHandler


class CodeReviewPluginHandler(PluginHandler):
    """Handler for /code-review plugin."""

    @property
    def plugin_name(self) -> str:
        return "code-review"

    def build_prompt(self, context: AgentContext) -> str:
        """Build prompt for /code-review with JIRA context."""
        pr_ref = ""
        if context.github_data:
            pr_ref = f" {context.github_data.pr_url}"
        parts = [f"/code-review medium --comment{pr_ref}"]

        if context.jira_tickets:
            jira_ids = ", ".join(t.id for t in context.jira_tickets)
            parts.append(f"\n\nLinked JIRA tickets: {jira_ids}")
            parts.append("Please review with context from these tickets:\n")
            for ticket in context.jira_tickets:
                parts.append(f"  - [{ticket.id}] {ticket.summary}")

        if context.github_data:
            gh = context.github_data
            parts.append(f"\n\nPR: {gh.pr_title}")
            parts.append(f"Author: {gh.author}")
            parts.append(f"Branch: {gh.head_branch}")

        return "".join(parts)

    def parse_output(self, output: str, context: AgentContext) -> dict:
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

        # Try to extract JSON array from ```json ... ``` block
        json_match = re.search(r'```json\s*(\[[\s\S]*?)\s*```', output, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                if isinstance(data, list) and len(data) > 0:
                    issues_found = True
                    issue_count = len(data)

                    from agent_runner import InlineComment, Review
                    comments = []
                    for item in data:
                        if isinstance(item, dict) and "file" in item and "line" in item:
                            severity = item.get("severity", "suggestion")
                            if severity not in ("error", "warning", "suggestion"):
                                severity = "suggestion"
                            comments.append(InlineComment(
                                file=str(item["file"]),
                                line=int(str(item["line"]).rstrip('L')),
                                severity=severity,
                                comment=item.get("summary", ""),
                            ))

                    review = Review(
                        summary=f"Code review found {issue_count} issue(s)",
                        inline_comments=comments,
                        verdict="changes_requested",
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
                review = None

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