"""Stage 1 — Overview pass: single LLM call, no tools.

Returns a 3-6 sentence plain-text summary of the PR's intent.
"""

import copy

from pr_review_performance.llm import chat


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


def run_overview(full_diff: str, pr_description: str, config) -> str:
    """Perform the overview pass.

    Returns the overview text. LLMError propagates: an unusable model API is fatal.
    """
    if pr_description:
        description_section = pr_description
    else:
        description_section = "No PR description was provided."

    prompt = OVERVIEW_PROMPT.format(
        pr_description=description_section,
        full_diff=full_diff,
    )

    messages = [{"role": "user", "content": prompt}]

    overview_config = copy.copy(config)
    overview_config.model = config.effective_overview_model
    response = chat(messages, tools=None, config=overview_config)
    if response.content:
        return response.content.strip()
    return "Overview not available."
