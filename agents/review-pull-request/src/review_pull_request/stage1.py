"""Stage 1 — Overview pass: single LLM call, no tools.

Returns a 3-6 sentence plain-text summary of the PR's intent.
"""

from review_pull_request.prompts import load_prompt
from review_pull_request.safe_diff import SafeDiff


OVERVIEW_PROMPT = load_prompt("overview.md")


def run_overview(diff: SafeDiff, pr_description: str, session) -> str:
    """Perform the overview pass.

    Takes a SafeDiff (never a raw diff string) and uses the session's model client.
    Returns the overview text. LLMError propagates: an unusable model API is fatal.
    """
    if pr_description:
        description_section = pr_description
    else:
        description_section = "No PR description was provided."

    prompt = OVERVIEW_PROMPT.format(
        pr_description=description_section,
        full_diff=diff.text,
    )

    messages = [{"role": "user", "content": prompt}]

    response = session.review_llm.chat(messages, tools=None)
    if response.content:
        return response.content.strip()
    return "Overview not available."
