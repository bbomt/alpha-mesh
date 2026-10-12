"""Stage 1 — Overview pass: single LLM call, no tools.

Returns a 3-6 sentence plain-text summary of the PR's intent.
"""

from review_pull_request.safe_diff import SafeDiff


OVERVIEW_PROMPT = """You are a senior engineer reviewing a pull request.
Given the PR description and full diff below, explain what these changes are doing
and what the intent seems to be.
Focus on the big picture: overall goals, architectural changes, key risks or concerns.
Do not report individual file issues — that happens in a later pass.
Write 3–6 sentences that establish the mental model of the change.
Output plain text only. Do not add headings, section titles, or markdown formatting.

## PR Description
{pr_description}

## Full Diff
{full_diff}
"""


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
