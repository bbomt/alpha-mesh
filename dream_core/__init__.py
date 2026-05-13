"""
Dream core logic - end-of-session memory consolidation.

This module provides the internal Python implementation for the dream feature.
The bin/dream shell script acts as a thin wrapper, calling into this module.

Usage (from bin/dream):
    python3 -m dream_core [command] [options]
"""

import sys
import json
import os
import re
import time
from collections import defaultdict, Counter
from typing import Dict, List, Optional, Any, Tuple

# Claude Code internal tags that indicate system-generated messages (not real user input)
CLAUDE_CODE_TAGS = (
    "<command-name", "<command-message", "<command-args",
    "<local-command-", "<system-reminder",
    "<local-command-stdout", "<local-command-stderr",
)


def is_real_user_message(text: str) -> bool:
    """Check if a user message is actual user input vs Claude Code system-generated."""

    if not text:
        return False

    stripped = text.lstrip()

    # System-generated messages START with these tags - filter them out
    # (user messages that QUOTE such tags should NOT be filtered)
    system_starts = (
        "<command-name",
        "<command-message",
        "<command-args",
        "<local-command-",
        "<local-command-stdout",
        "<local-command-stderr",
        "<system-reminder",
        "<local-command-caveat",
    )
    for tag in system_starts:
        if stripped.startswith(tag):
            return False

    # Check for system-generated content patterns (must be near the start)
    system_patterns = (
        "## Context Usage",
        "**Model:**",
        "**Tokens:**",
        "### Estimated usage by category",
        "| Category | Tokens | Percentage |",
        "This session is being continued from a previous conversation",
        "Summary:",
        "Primary Request and Intent:",
    )
    for pattern in system_patterns:
        idx = text.find(pattern)
        if idx >= 0 and idx < 100:  # Pattern near start = system-generated
            return False

    return True

MESH_SUBDIR = ".mesh"
MEMORY_FILENAME = "MEMORY.md"
MAX_MEMORY_LINES = 200
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
SECTION_FIELDS = ["architecture", "patterns", "jira_workflow",
                  "deploy_commands", "testing", "workflow"]

# Section headers for 3-layer memory model
LAYER1_HEADER = "## Identity"
LAYER2_HEADER = "## Current State"
LAYER3_HEADER = "## Recent Signals"
CONFLICTS_HEADER = "## ⚠️ Conflicts"
REJECTED_HEADER = "### Rejected Decisions"
REPO_LOCAL_TAG = "[REPO-LOCAL]"
GLOBAL_TAG = "[GLOBAL]"

def filter_empty(sections):
    """Remove empty sections (headers with no bullet points following)."""
    result = []
    i = 0
    while i < len(sections):
        line = sections[i]
        if line.startswith("### "):
            # Check if next non-blank lines are bullets
            j = i + 1
            while j < len(sections) and (sections[j].strip() == "" or sections[j].strip().startswith("<!--")):
                j += 1
            has_content = j < len(sections) and sections[j].strip().startswith("-")
            if has_content:
                result.extend(sections[i:j])
            i = j
        else:
            result.append(line)
            i += 1
    return result


def split_routing(extracted: str) -> Tuple[str, str]:
    """
    Split extracted insights into REPO-LOCAL and GLOBAL markdown strings.

    Args:
        extracted: Raw extracted insights from LLM

    Returns:
        Tuple of (repo_local_md, global_md)
    """
    repo_local = []
    global_md = []

    in_rejected = False

    for line in extracted.split("\n"):
        # Track section headers - add to both
        if line.startswith("### "):
            in_rejected = "Rejected Decisions" in line
            repo_local.append(line)
            global_md.append(line)
            continue

        # Keep comments and blank lines in both
        if line.strip().startswith("<!--") or line.strip() == "":
            repo_local.append(line)
            global_md.append(line)
            continue

        # Rejected Decisions go to both (permanent record)
        if in_rejected:
            repo_local.append(line)
            global_md.append(line)
            continue

        # Route based on tag
        if REPO_LOCAL_TAG in line:
            repo_local.append(line)
        elif GLOBAL_TAG in line:
            global_md.append(line)
        else:
            # Untagged lines: default to REPO-LOCAL only (keep local when unsure)
            repo_local.append(line)

    # Filter out empty sections
    repo_local = filter_empty(repo_local)
    global_md = filter_empty(global_md)

    return "\n".join(repo_local).strip(), "\n".join(global_md).strip()


def validate_routing(extracted: str, repo_name: str) -> str:
    """
    Post-extraction validation to catch obvious mis-taggings.

    If a [GLOBAL] tagged line contains the repo name, it's likely mis-tagged
    and should be [REPO-LOCAL].

    Args:
        extracted: Raw extracted insights from LLM
        repo_name: Name of the repo (e.g., "alpha-mesh")

    Returns:
        Corrected extracted content
    """
    lines = extracted.split("\n")
    corrected = []
    corrections = 0

    for line in lines:
        # Check if line is tagged GLOBAL but contains repo name
        if GLOBAL_TAG in line and repo_name in line:
            # Re-tag as REPO-LOCAL
            corrected_line = line.replace(GLOBAL_TAG, REPO_LOCAL_TAG, 1)
            corrected.append(corrected_line)
            corrections += 1
            print(f"  [validate] Re-tagged GLOBAL→REPO-LOCAL: {line[:80]}...", flush=True, file=sys.stderr)
        else:
            corrected.append(line)

    if corrections > 0:
        print(f"  [validate] Fixed {corrections} mis-tagged line(s) containing '{repo_name}'", flush=True, file=sys.stderr)

    return "\n".join(corrected)


def enforce_recent_signals_cap(memory_content: str, max_entries: int = 5) -> str:
    """
    Enforce a max number of entries in ## Recent Signals section.

    Args:
        memory_content: Full MEMORY.md content
        max_entries: Maximum number of entries to keep (default 5)

    Returns:
        Updated memory content with cap enforced
    """
    lines = memory_content.split("\n")
    result = []
    in_recent_signals = False
    signals = []

    for line in lines:
        if line.startswith(LAYER3_HEADER):
            in_recent_signals = True
            result.append(line)
            continue
        if in_recent_signals and line.startswith("## "):
            # New section starts, process collected signals
            in_recent_signals = False
            # Keep only last max_entries
            for sig in signals[-max_entries:]:
                result.append(sig)
            signals = []
            result.append(line)
            continue
        if in_recent_signals:
            if line.strip().startswith("-"):
                signals.append(line)
            else:
                result.append(line)
        else:
            result.append(line)

    # Handle if file ends with Recent Signals section
    if in_recent_signals:
        for sig in signals[-max_entries:]:
            result.append(sig)

    return "\n".join(result)


def check_conflicts(memory_content: str) -> Tuple[bool, List[str]]:
    """
    Check if ## ⚠️ Conflicts section is non-empty.

    Returns:
        Tuple of (has_conflicts, list of conflict entries)
    """
    conflicts = []
    in_conflicts = False
    for line in memory_content.split("\n"):
        if line.startswith(CONFLICTS_HEADER):
            in_conflicts = True
            continue
        if in_conflicts and line.startswith("## "):
            in_conflicts = False
            continue
        if in_conflicts and line.strip() and not line.strip().startswith("<!--"):
            conflicts.append(line)
    return len(conflicts) > 0, conflicts



def extract_sessions_from_jsonl(
    projects_dir: str,
    cutoff: str,
    target_repo: Optional[str] = None,
    include_assistant: bool = False
) -> Dict[str, Dict[str, Any]]:
    """
    Scan JSONL session files and extract content by repo.

    Args:
        projects_dir: Path to ~/.claude/projects
        cutoff: Date string (YYYY-MM-DD) - only process files modified since
        target_repo: Optional repo path to filter results.
            When set to a parent directory (e.g., ~/environment/pmat),
            all child repos are included so multi-repo work is captured.
        include_assistant: If True, include assistant messages. Default False (user only).

    Returns:
        Dict mapping repo_path -> {"content": str, "dates": List[str], "file_count": int}
    """
    repo_data = defaultdict(lambda: {"content_lines": [], "dates": set(), "file_count": 0, "message_count": 0})

    if not os.path.isdir(projects_dir):
        return {}

    for root, dirs, files in os.walk(projects_dir):
        for fname in files:
            if not fname.endswith('.jsonl'):
                continue
            fpath = os.path.join(root, fname)

            try:
                mtime = os.path.getmtime(fpath)
                fdate = time.strftime('%Y-%m-%d', time.localtime(mtime))
            except OSError:
                continue

            if fdate < cutoff:
                continue

            cwds = set()
            messages = []
            recent_messages = []  # For deduplication of similar consecutive messages

            try:
                with open(fpath) as f:
                    for line_counter, line in enumerate(f, 1):
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        cwd = obj.get('cwd')
                        if cwd:
                            cwds.add(cwd)

                        # Handle both regular messages and queue-operation (user input)
                        role = ''
                        content = ''
                        
                        # Check if this is a queue-operation (user typed a message)
                        if obj.get('type') == 'queue-operation' and obj.get('operation') == 'enqueue':
                            # This is a user message sent via the queue
                            role = 'user'
                            content = obj.get('content', '')
                        else:
                            # Regular message format
                            msg = obj.get('message', obj)
                            if 'role' not in msg:
                                msg = obj
                            role = msg.get('role', '')
                            content = msg.get('content', '')

                        # Skip user messages that are system-generated (start with command/local tags)
                        if role == 'user' and isinstance(content, str):
                            stripped = content.strip()
                            if stripped.startswith(('<command-', '<local-command-', '<system-reminder>')):
                                continue

                        # Process assistant messages only if include_assistant is True
                        if include_assistant and role == 'assistant' and isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict) and block.get('type') == 'text':
                                    text = block.get('text', '').strip()
                                    if text and len(text) > 100:
                                        messages.append(f"[assistant] {text[:800]}")

                        elif role == 'user':
                            text = ''
                            if isinstance(content, str):
                                text = content.strip()
                            elif isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict) and block.get('type') == 'text':
                                        text = block.get('text', '').strip()
                                        break
                            if text and len(text) > 20 and not text.startswith('/') and is_real_user_message(text):
                                # Truncate for consistency
                                truncated = text[:300]
                                # Skip if very similar to last message (within same session)
                                if recent_messages and recent_messages[-1].startswith('[user]'):
                                    last_msg = recent_messages[-1][7:]  # strip [user] prefix
                                    # If messages are >80% identical, skip this one
                                    if truncated.startswith(last_msg[:50]) and len(truncated) <= len(last_msg) + 10:
                                        continue
                                recent_messages.append(f"[user] {truncated}")
                                messages.append(f"[user] {truncated}")

                if not messages:
                    continue

                # Determine repo path from most common cwd
                if cwds:
                    repo_path = Counter(cwds).most_common(1)[0][0]
                else:
                    proj_dir_name = os.path.basename(root)
                    decoded = proj_dir_name.lstrip('-')
                    home = os.path.expanduser('~')
                    candidate = os.path.join(home, decoded.replace('-', '/'))
                    repo_path = candidate if os.path.isdir(candidate) else proj_dir_name

                # Filter to target repo if specified (exact path match only)
                if target_repo:
                    real_target = os.path.realpath(os.path.expanduser(target_repo))
                    real_repo = os.path.realpath(repo_path)
                    # Only include the exact target repo (no child repos)
                    if real_repo != real_target:
                        continue

                repo_data[repo_path]["file_count"] += 1

                if messages:
                    content_str = '\n'.join(messages)
                    repo_data[repo_path]["content_lines"].append(
                        f"--- Session: {os.path.basename(root)} ({fdate}) ---\n{content_str}"
                    )
                    repo_data[repo_path]["dates"].add(fdate)
                    repo_data[repo_path]["message_count"] += len(messages)

            except OSError:
                continue

    result = {}
    for repo_path, data in repo_data.items():
        if data["content_lines"]:
            message_count = data.get("message_count", 0)
            result[repo_path] = {
                "content": '\n\n'.join(data["content_lines"]),
                "dates": sorted(data["dates"]),
                "file_count": data["file_count"],
                "session_count": len(data["content_lines"]),
                "message_count": message_count
            }

    return result


def call_llm(
    prompt: str,
    model: str,
    token: str,
    base_url: str,
    max_tokens: int = 2000
) -> Optional[str]:
    """
    Call the LLM using OpenAI SDK.

    Args:
        prompt: The prompt text to send
        model: Model name
        token: API token
        base_url: API base URL
        max_tokens: Maximum tokens in response

    Returns:
        Response text or None on failure
    """
    try:
        from openai import OpenAI

        # Adjust base URL for OpenAI SDK compatibility:
        # 1. MiniMax uses /anthropic suffix -> convert to /v1
        # 2. Other providers (OpenRouter, etc.) need /v1 appended if missing
        adjusted_base_url = base_url
        if base_url:
            stripped = base_url.rstrip("/")
            if stripped.endswith("/anthropic"):
                adjusted_base_url = stripped.replace("/anthropic", "") + "/v1"
            elif not stripped.endswith("/v1"):
                # Append /v1 for OpenAI-compatible APIs that don't have it
                adjusted_base_url = stripped + "/v1"

        client = OpenAI(
            api_key=token,
            base_url=adjusted_base_url if adjusted_base_url else None
        )

        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens
        )

        if response.choices and response.choices[0].message.content:
            return response.choices[0].message.content

    except Exception as e:
        print(f"LLM call failed: {e}", file=sys.stderr)

    return None


def extract_response_text(response_text: str) -> str:
    """
    Extract text content from LLM response.

    Handles:
    - Thinking blocks (<think>... -->) — stripped, content after used
    - JSON with "content" field (structured output mode)
    - JSON with code fences (```json ... ```)
    - Plain text fallback

    Args:
        response_text: Raw response text

    Returns:
        Extracted text or empty string
    """
    if not response_text:
        return ""

    # Strip thinking blocks: first remove the whole thinking block,
    # but preserve what comes after it (the actual response)
    # MiniMax: <think>...</think> and <!-- ... -->
    response_text = re.sub(r'<think>.*?</think>', '', response_text, flags=re.DOTALL)
    response_text = re.sub(r'<!--.*?-->', '', response_text, flags=re.DOTALL)

    # Extract JSON from markdown code block: ```json ... ```
    json_match = re.search(r'```json\s*(.*?)\s*```', response_text, re.DOTALL)
    if json_match:
        response_text = json_match.group(1).strip()
    else:
        # Fallback: find first brace
        brace_pos = response_text.find('{')
        if brace_pos >= 0:
            response_text = response_text[brace_pos:]
    response_text = response_text.strip()

    # Try parsing as JSON
    try:
        r = json.loads(response_text)
        if isinstance(r, dict):
            # Structured output with named fields (architecture, patterns, etc.)
            if any(k in r for k in SECTION_FIELDS):
                lines = []
                for field in SECTION_FIELDS:
                    value = r.get(field)
                    if value is None or value == "":
                        continue
                    heading = "### " + field.replace("_", " ").title()
                    lines.append(heading)
                    if isinstance(value, list):
                        for item in value:
                            if item:
                                lines.append(str(item).strip())
                    else:
                        lines.append(str(value).strip())
                    lines.append("")
                return "\n".join(lines).strip()
            # OpenRouter nested: { "learnings": { "Architecture": [...], "Workflow": [...] } }
            learnings = r.get("learnings")
            if isinstance(learnings, dict):
                lines = []
                # O(n+m) instead of O(n*m): build lowercase key map once
                learnings_lower = {k.lower(): v for k, v in learnings.items()}
                for field in SECTION_FIELDS:
                    value = learnings_lower.get(field)
                    if value is None or value == "":
                        continue
                    heading = "### " + field.replace("_", " ").title()
                    lines.append(heading)
                    if isinstance(value, list):
                        for item in value:
                            if item:
                                lines.append(str(item).strip())
                    else:
                        lines.append(str(value).strip())
                    lines.append("")
                if lines:
                    return "\n".join(lines).strip()
            # Legacy: content field
            if "content" in r and isinstance(r["content"], str):
                return r["content"].strip()
            # Anthropic content blocks: { "content": [{"type": "text", "text": "..."}] }
            val = _extract_from_anthropic_block(r)
            if val:
                return val
            # OpenAI choices format
            val = _extract_from_openai_choices(r)
            if val:
                return val
            # OpenRouter wrapped: { "response"|"text"|"answer": "actual markdown" }
            for field in ("response", "text", "answer"):
                val = r.get(field)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            # OpenRouter validation wrapper: { "is_valid": true, ... } — skip
            if r.get("is_valid") is True and len(r) <= 3:
                return ""
    except json.JSONDecodeError:
        pass

    # Plain text fallback (markdown returned directly, or unparseable)
    return response_text.strip()


def _extract_from_anthropic_block(r: dict) -> Optional[str]:
    """Extract from Anthropic content blocks format."""
    content = r.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "").strip()
    return None


def _extract_from_openai_choices(r: dict) -> Optional[str]:
    """Extract from OpenAI choices format."""
    choices = r.get("choices")
    if isinstance(choices, list) and len(choices) > 0:
        msg = choices[0].get("message", {})
        if isinstance(msg, dict):
            return msg.get("content", "").strip()
    return None


def _handle_repo_result(memory_file, updated_memory, repo_path, repo_name,
                        global_learnings, cross_repo_lines, dry_run, status):
    """Write memory, enforce caps, check conflicts, collect global."""
    updated_memory = enforce_recent_signals_cap(updated_memory, max_entries=5)
    has_conflicts, _ = check_conflicts(updated_memory)
    if has_conflicts:
        print(f"  WARNING: Conflicts section non-empty in {memory_file}", file=sys.stderr)
    if not dry_run:
        with open(memory_file, "w") as f:
            f.write(updated_memory)
    if global_learnings:
        cross_repo_lines.append(global_learnings)
    return (repo_path, repo_name, status)


def process_repos(
    repos_data: Dict[str, Dict[str, Any]],
    model_config: Dict[str, str],
    date: str,
    dry_run: bool = False
) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    """
    Process each repo: extract learnings and synthesize memory.

    Args:
        repos_data: Dict from extract_sessions_from_jsonl
        model_config: Dict with keys: model, token, base_url
        date: Current date string
        dry_run: If True, don't write files

    Returns:
        Tuple of (summary_list, cross_repo_content)
    """
    summary = []
    cross_repo_lines = []

    for repo_path, info in sorted(repos_data.items()):
        repo_name = os.path.basename(repo_path)
        dates_str = ', '.join(info['dates'])

        print(f"Processing: {repo_name} ({dates_str})", flush=True)

        # Step A: Extract learnings
        print(f"  Extracting learnings...", flush=True, file=sys.stderr)
        new_learnings = extract_learnings(
            session_content=info["content"],
            repo_name=repo_name,
            date=date,
            model_config=model_config
        )

        # Validate routing (catch mis-tagged content)
        new_learnings = validate_routing(new_learnings, repo_name)

        # Split into REPO-LOCAL and GLOBAL insights
        repo_local_learnings, global_learnings = split_routing(new_learnings)

        # Step B: Read existing memory
        mesh_dir = os.path.join(repo_path, MESH_SUBDIR)
        memory_file = os.path.join(mesh_dir, MEMORY_FILENAME)
        existing_memory = ""

        if not dry_run:
            os.makedirs(mesh_dir, exist_ok=True)
            if os.path.isfile(memory_file):
                with open(memory_file) as f:
                    existing_memory = f.read()

        # Step C: Synthesize
        if model_config["model"] and model_config["token"]:
            has_existing = bool(existing_memory)
            print(f"  Synthesizing{' with existing memory...' if has_existing else ' (new memory)...'}", flush=True, file=sys.stderr)

            updated_memory = synthesize_memory(
                existing_memory=existing_memory,
                new_learnings=repo_local_learnings,
                date=date,
                repo_name=repo_name,
                model_config=model_config
            )

            if updated_memory:
                status = "written" if has_existing else "new"
                summary.append(_handle_repo_result(
                    memory_file, updated_memory, repo_path, repo_name,
                    global_learnings, cross_repo_lines, dry_run, status))
            else:
                # Fallback
                fallback_content = f"\n\n## {date}\n{new_learnings}"
                if not dry_run:
                    if has_existing:
                        with open(memory_file, "a") as f:
                            f.write(fallback_content)
                    else:
                        with open(memory_file, "w") as f:
                            f.write(f"# MEMORY.md — {repo_name}\n_Last updated: {date}_\n")
                            f.write("\n## Identity\n\n## Current State\n\n## Recent Signals\n\n## ⚠️ Conflicts\n")
                        with open(memory_file, "a") as f:
                            f.write(f"\n\n## Raw Learnings\n{repo_local_learnings}")
                status = "append-fallback" if has_existing else "new-fallback"
                summary.append((repo_path, repo_name, status))
                if global_learnings:
                    cross_repo_lines.append(global_learnings)
        else:
            # LLM unavailable - raw content fallback
            print(f"  LLM unavailable - writing raw content", flush=True, file=sys.stderr)
            raw_content = f"\n\n## {date} — Raw session content (LLM unavailable)\n{new_learnings}"
            if not dry_run:
                with open(memory_file, "a") as f:
                    f.write(raw_content)
            summary.append((repo_path, repo_name, "raw-fallback"))
            if global_learnings:
                cross_repo_lines.append(global_learnings)

    return summary, "\n".join(cross_repo_lines) if cross_repo_lines else None

def extract_learnings(
    session_content: str,
    repo_name: str,
    date: str,
    model_config: Dict[str, str]
) -> str:
    """
    Extract structured learnings from raw session content using LLM.

    Args:
        session_content: Raw session text
        repo_name: Name of the repo
        date: Current date
        model_config: Model configuration dict

    Returns:
        Extracted learnings text
    """
    extract_prompt = f"""# Role
You are a Knowledge Distillation Engine for a multi-repo software development team.
Your job is NOT to summarise what happened in this session. Your job is to extract
durable, actionable knowledge and classify it correctly so a memory system stays
useful as it accumulates — rather than becoming noisier over time.

# What to Capture
Focus on the "How" and "Why". Look for:
- Repeatable patterns: how the team structures work, names things, or organises code
- Rejected decisions and the reason they were rejected (prevents re-litigating)
- JIRA/ticket workflow specifics: what triggers a branch, what a PR requires, deploy conditions
- Friction signals: moments where Claude Code was corrected, asked to redo something,
  or redirected — these reveal implicit rules that haven't been written down yet
- Testing boundaries: what counts as a unit test vs integration test in this project,
  what gets mocked and what does not
- Phrases like "we always...", "in this project...", "don't do X, do Y instead"

# What to Ignore
Do not extract:
- Specific bug fixes or their code
- One-off data migrations or scripts
- Any fact that would be wrong or irrelevant within 48 hours
- General programming advice that applies everywhere (not specific to this team/repo)

# Layer Classification
For every insight you extract, assign it to exactly one layer:

LAYER 1 — DURABLE IDENTITY
  Changes rarely. Architectural principles, conventions, workflow rules, philosophy.

LAYER 2 — CURRENT STATE
  Changes weekly. Active work, known workarounds, in-progress decisions.
  Must include an expiry condition (e.g. "until X is resolved").

LAYER 3 — SESSION SIGNAL
  Expires within ~7 days. One sentence. Max 3 per session.

# Routing Classification — MANDATORY TAGS
Every bullet MUST start with a routing tag, no exceptions:
- `[REPO-LOCAL]`: specific to this codebase's tech stack, folder structure, or domain logic
- `[GLOBAL]`: applies across all repos (team process, JIRA workflow, communication norms)

## Critical Routing Rules
1. **If the insight mentions the repo name "{repo_name}", it MUST be `[REPO-LOCAL]`**
2. **When in doubt, keep it local** — global memory that grows too large loses value.
3. **GLOBAL means truly cross-repo**: team-wide JIRA settings, communication norms, shared CI/CD patterns

## Examples (study these carefully)
### REPO-LOCAL (correct tagging):
- `[REPO-LOCAL] Log Path: Commands are logged to ~/environment/logs/ai/claude/{repo_name}.log`
- `[REPO-LOCAL] Testing: uses pnpm test:unit (not npm or yarn)`
- `[REPO-LOCAL] Architecture: single-spa MFE pattern with 6 sub-repos`
- `[REPO-LOCAL] Claude Code uses --print flag (not --headless)`

### GLOBAL (correct tagging):
- `[GLOBAL] JIRA workflow: move ticket to IN PROGRESS when starting work`
- `[GLOBAL] Git commits: format is feature|fix[{{summary}}{{JIRA-XX}}]: description`
- `[GLOBAL] Team uses AWS CDK for infrastructure, SAM for local testing`

If an insight is not clearly cross-repo team knowledge, tag it `[REPO-LOCAL]`.

# Session Source: {repo_name} | Date: {date}

{session_content}

# Output Format (Markdown only — no preamble)

## Extracted Insights

### Layer 1 — Durable Identity
<!-- One bullet per insight. Format: `[REPO-LOCAL]` or `[GLOBAL]` — TAG IS MANDATORY, no untagged bullets -->

### Layer 2 — Current State
<!-- One bullet per insight. Format: `[REPO-LOCAL]` or `[GLOBAL]` Insight text. (Expires: condition) — TAG IS MANDATORY -->

### Layer 3 — Session Signals
<!-- Max 3 bullets. Format: `[REPO-LOCAL]` or `[GLOBAL]` One sentence. — TAG IS MANDATORY -->

### Rejected Decisions
<!-- Decisions explicitly ruled out. Format: Decision — Reason. -->
<!-- This section is high value. Do not skip it if any rejections occurred. -->"""

    base_url = model_config.get("base_url", "") or "https://api.anthropic.com"

    result = call_llm(
        prompt=extract_prompt,
        model=model_config["model"],
        token=model_config["token"],
        base_url=base_url,
        max_tokens=2000
    )

    if result:
        extracted = extract_response_text(result)
        if extracted and extracted.strip():
            return extracted

    return session_content


def synthesize_memory(
    existing_memory: str,
    new_learnings: str,
    date: str,
    repo_name: str,
    model_config: Dict[str, str]
) -> Optional[str]:
    """
    Synthesize existing memory with new learnings.

    Args:
        existing_memory: Current memory file content
        new_learnings: New learnings to merge
        date: Current date
        repo_name: Name of the repo
        model_config: Model configuration dict

    Returns:
        Updated memory content, or None on failure.
    """
    sys_prompt = f"""# Role
You are a Memory Curator for a software development team's AI coding assistant.
You will be given two inputs:
1. EXISTING MEMORY — the current contents of MEMORY.md
2. NEW INSIGHTS — structured extraction from today's session(s)

Your task is to produce an updated MEMORY.md that is more useful than the current one.
More useful means: more accurate, less redundant, no stale facts, conflicts surfaced.

# Merge Rules

## For Layer1 — Durable Identity
- If a new insight CONFIRMS an existing fact: skip it (no duplicate)
- If a new insight REFINES an existing fact: update it in place, append `(updated {date})`
- If a new insight CONTRADICTS an existing fact: do NOT silently overwrite.
  Flag it in the Conflicts section and keep BOTH versions until a human resolves it.

## For Layer2 — Current State
- Replace outdated current-state facts with new ones where the expiry condition has
  clearly been met (e.g. a workaround that was resolved)
- If unclear whether the old fact expired, keep both and flag in Conflicts

## For Layer3 — Session Signals
- Prepend new signals
- Drop oldest entries beyond the 5-entry cap

## For Rejected Decisions
- Accumulate these permanently in Layer1 under a `### Rejected Decisions` subsection
- They never expire — do not drop them during a merge

# Conflict Protocol
Never silently overwrite a memory. If new content contradicts existing content:
- Add an entry to the `## ⚠️ Conflicts` section
- Format: `[Date] {repo_name}: NEW fact vs EXISTING fact — awaiting resolution`
- Leave both facts in place under their respective layers
- A human or a future session will resolve it

# Routing
- Insights marked REPO-LOCAL go only into the repo's `.mesh/MEMORY.md`
- Insights marked GLOBAL go only into the global memory file
- Do not mix them

# Inputs

## EXISTING MEMORY
{existing_memory}

## NEW INSIGHTS
{new_learnings}

# Output

Produce the complete updated MEMORY.md file using the structure below.
Do not include any commentary outside the file content itself.

---

# MEMORY.md — {repo_name}
_Last updated: {date}_

## Identity
<!-- Layer1: Durable facts about how this team/repo works. -->

### Workflow & Tooling
<!-- Branching, JIRA, deploy triggers, PR conventions -->

### Testing Philosophy
<!-- Unit vs integration boundary, mocking preferences, coverage expectations -->

### Architecture & Patterns
<!-- Folder structure, naming conventions, state management, the "golden path" -->

### Tribal Knowledge
<!-- Non-standard practices vital to this repo. Things that would surprise a new developer. -->

### Rejected Decisions
<!-- Decisions ruled out and why. Permanent record. -->

## Current State
<!-- Layer2: Active work, workarounds, in-progress decisions. Each has an expiry condition. -->

## Recent Signals
<!-- Layer3: Rolling window of 5. Session-level discoveries. -->

## ⚠️ Conflicts
<!-- Unresolved contradictions between existing and new memory. Human review required. -->"""

    base_url = model_config.get("base_url", "") or "https://api.anthropic.com"

    result = call_llm(
        prompt=sys_prompt,
        model=model_config["model"],
        token=model_config["token"],
        base_url=base_url,
        max_tokens=3000
    )

    if not result:
        return None

    updated_memory = extract_response_text(result)
    if not updated_memory:
        return None

    # Extract cross-repo items
    return updated_memory


def create_new_memory(new_learnings: str, date: str, repo_name: str) -> str:
    """
    Create a new memory file from scratch.

    Args:
        new_learnings: Content to write
        date: Current date
        repo_name: Repo name for header

    Returns:
        Memory file content
    """
    return f"""# Memory: {repo_name}

## {date}
{new_learnings}
"""


def write_cross_repo_memory(
    cross_content: str,
    date: str,
    model_config: Dict[str, str],
    dry_run: bool = False
) -> bool:
    """
    Write cross-repo learnings to ~/.mesh/MEMORY.md.

    Args:
        cross_content: Content to write
        date: Current date
        model_config: Model configuration
        dry_run: If True, don't actually write

    Returns:
        True if successful
    """
    if not cross_content:
        return True

    local_cross_file = os.path.join(os.path.expanduser("~"), ".mesh", MEMORY_FILENAME)
    local_cross_existing = ""

    if os.path.isfile(local_cross_file):
        with open(local_cross_file) as f:
            local_cross_existing = f.read()

    # Use GLOBAL insights directly (routing done in process_repos)
    clean_content = cross_content

    sys_prompt = f"""# Role
You are a Memory Curator for a software development team's AI coding assistant.
You will be given two inputs:
1. EXISTING MEMORY — the current contents of the global MEMORY.md
2. NEW INSIGHTS — structured extraction marked as GLOBAL

Your task is to produce an updated global MEMORY.md that is more useful than the current one.

# Merge Rules

## For Layer1 — Durable Identity
- If a new insight CONFIRMS an existing fact: skip it (no duplicate)
- If a new insight REFINES an existing fact: update it in place, append `(updated {date})`
- If a new insight CONTRADICTS an existing fact: do NOT silently overwrite.
  Flag it in the Conflicts section and keep BOTH versions until a human resolves it.

## For Layer2 — Current State
- Replace outdated current-state facts with new ones where the expiry condition has
  clearly been met
- If unclear whether the old fact expired, keep both and flag in Conflicts

## For Layer3 — Session Signals
- Prepend new signals
- Drop oldest entries beyond the 5-entry cap

## For Rejected Decisions
- Accumulate these permanently in Layer1 under a `### Rejected Decisions` subsection
- They never expire — do not drop them during a merge

# Conflict Protocol
Never silently overwrite a memory. If new content contradicts existing content:
- Add an entry to the `## ⚠️ Conflicts` section
- Format: `[Date] GLOBAL: NEW fact vs EXISTING fact — awaiting resolution`
- Leave both facts in place under their respective layers

# Inputs

## EXISTING MEMORY
{local_cross_existing if local_cross_existing else "No existing memory."}

## NEW INSIGHTS
{clean_content}

# Output

Produce the complete updated global MEMORY.md file using the structure below.
Do not include any commentary outside the file content itself.

---

# MEMORY.md — GLOBAL
_Last updated: {date}_

## Identity
<!-- Layer1: Durable facts about team process, JIRA workflow, communication norms. -->

### Workflow & Tooling
<!-- Team-wide branching, JIRA, deploy triggers, PR conventions -->

### Testing Philosophy
<!-- Team-wide testing standards, mocking preferences -->

### Architecture & Patterns
<!-- Cross-repo architectural principles, shared patterns -->

### Tribal Knowledge
<!-- Team norms that would surprise a new developer. -->

### Rejected Decisions
<!-- Team-wide decisions ruled out and why. Permanent record. -->

## Current State
<!-- Layer2: Team-wide active work, workarounds, in-progress decisions. -->

## Recent Signals
<!-- Layer3: Rolling window of 5. Team-level session discoveries. -->

## ⚠️ Conflicts
<!-- Unresolved contradictions. Human review required. -->
"""

    base_url = model_config.get("base_url", "") or "https://api.anthropic.com"

    result = call_llm(
        prompt=sys_prompt,
        model=model_config["model"],
        token=model_config["token"],
        base_url=base_url,
        max_tokens=3000
    )

    if not dry_run:
        os.makedirs(os.path.dirname(local_cross_file), exist_ok=True)

        if result:
            updated = extract_response_text(result)
            if updated:
                # Enforce 5-entry cap on Recent Signals
                updated = enforce_recent_signals_cap(updated, max_entries=5)
                # Check for conflicts
                has_conflicts, _ = check_conflicts(updated)
                if has_conflicts:
                    print(f"  ⚠️  WARNING: Conflicts section non-empty in {local_cross_file}", file=sys.stderr)
                with open(local_cross_file, "w") as f:
                    f.write(updated)
                return True
            else:
                # Fallback: append raw
                with open(local_cross_file, "a") as f:
                    f.write(f"\n\n## {date}\n{cross_content}")
                return True
        else:
            # LLM unavailable
            with open(local_cross_file, "a") as f:
                f.write(f"\n\n## {date}\n{cross_content}")
            return True

    return True


def main():
    """Main entry point for dream_core module - unified CLI."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Dream core logic - end-of-session memory consolidation.",
        epilog="Model config can be set via env vars or CLI args."
    )
    parser.add_argument("--dry-run", action="store_true", help="Print without writing")
    parser.add_argument("--days", type=int, default=0, help="Days back to process")
    parser.add_argument("--repo", type=str, default="", help="Filter to specific repo")
    parser.add_argument("--include-assistant", action="store_true",
                        help="Include assistant messages (default: user only)")
    parser.add_argument("--projects-dir", type=str, default=CLAUDE_PROJECTS)
    parser.add_argument("--model", type=str, default=os.environ.get("DREAM_MODEL", ""))
    parser.add_argument("--token", type=str, default=os.environ.get("DREAM_TOKEN", ""))
    parser.add_argument("--base-url", type=str, default=os.environ.get("DREAM_URL", ""))

    args = parser.parse_args()

    # Determine cutoff date
    if args.days > 0:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - args.days * 86400))
    else:
        cutoff = time.strftime("%Y-%m-%d")

    date = time.strftime("%Y-%m-%d")

    # ─── 1. Extract sessions ───────────────────────────────────────────────────
    print(f"Scanning {args.projects_dir} ...", file=sys.stderr)

    repos_data = extract_sessions_from_jsonl(
        projects_dir=args.projects_dir,
        cutoff=cutoff,
        target_repo=args.repo if args.repo else None,
        include_assistant=args.include_assistant
    )

    file_count = sum(info.get('file_count', 0) for info in repos_data.values())
    if file_count == 0:
        print(f"No session files found modified since {cutoff}.", file=sys.stderr)
        print("dream complete — nothing to process.", file=sys.stderr)
        return 0

    print(f"Found {file_count} session file(s)", file=sys.stderr)

    # Warn if session files are stale (might miss recent messages)
    now = time.time()
    for repo_path, info in repos_data.items():
        # Check if any session file is older than 5 minutes
        stale_files = []
        for root, dirs, files in os.walk(args.projects_dir if 'args' in dir() else CLAUDE_PROJECTS):
            for fname in files:
                if not fname.endswith('.jsonl'):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    if now - mtime > 300:  # 5 minutes
                        stale_files.append(fname)
                except OSError:
                    continue
        if stale_files:
            print(f"  WARNING: Some session files are >5 min old — recent messages might be missing.", file=sys.stderr)
            print(f"  For best results, exit Claude Code (to flush) then run mesh dream.", file=sys.stderr)
            break

    # Print user messages to console before sending to LLM
    if repos_data:
        total_repos = len(repos_data)
        total_sessions = sum(info.get('session_count', 0) for info in repos_data.values())
        total_messages = sum(info.get('message_count', 0) for info in repos_data.values())
        print(f"\n>>> ======== USER MESSAGES (start:) ========= \n\n\tLLM INPUT: {total_repos} repos, {total_sessions} sessions, {total_messages} user messages\n")
        for repo_path, info in sorted(repos_data.items()):
            repo_name = os.path.basename(repo_path)
            session_count = info.get('session_count', 0)
            message_count = info.get('message_count', 0)
            print(f"  REPO: {repo_name} | Sessions: {session_count} | Messages: {message_count}")
            print(f"  Date: {info['dates'][0] if info.get('dates') else 'unknown'}")
            print(f"  ─────────────────────────────────────────────────────────")
            print(info["content"])
            print("")
        print(f">>> ======== USER MESSAGES (end:) ========= \n\n\tEND OF LLM INPUT ({total_messages} messages total) ===\n")

    if not repos_data:
        return 0

    # ─── 2. Dry run ────────────────────────────────────────────────────────────
    if args.dry_run:
        print("")
        print("=== DRY RUN — printing extracted learnings, writing nothing ===")
        print("")
        for i, (repo, info) in enumerate(sorted(repos_data.items()), 1):
            dates_str = ', '.join(info['dates'])
            content_preview = info['content'][:800]
            print(f"[Repo {i}] {repo}")
            print(f"  Sessions dates: {dates_str}")
            print(f"  Content ({len(info['content'])} chars):")
            print(f"  {content_preview}")
            print("")
        print("=== DRY RUN COMPLETE — no files written ===")
        return 0

    # ─── 3. Validate model config ──────────────────────────────────────────────
    if not args.model or not args.token:
        print("ERROR: DREAM_MODEL and DREAM_TOKEN must be set (via env or --model/--token)", file=sys.stderr)
        return 1

    raw_base_url = args.base_url or "https://api.minimax.io/v1"

    # Apply same adjustments as call_llm for consistency in logging
    adjusted_base_url = raw_base_url
    if raw_base_url:
        stripped = raw_base_url.rstrip("/")
        if stripped.endswith("/anthropic"):
            adjusted_base_url = stripped.replace("/anthropic", "") + "/v1"
        elif not stripped.endswith("/v1"):
            adjusted_base_url = stripped + "/v1"

    model_config = {
        "model": args.model,
        "token": args.token,
        "base_url": adjusted_base_url
    }

    print(f"Using model: {args.model}  |  API: {adjusted_base_url}", file=sys.stderr)

    # ─── 4. Process repos ──────────────────────────────────────────────────────
    print("Processing repos...", file=sys.stderr)

    summary, cross_content = process_repos(
        repos_data=repos_data,
        model_config=model_config,
        date=date,
        dry_run=False
    )

    # ─── 5. Cross-repo memory ──────────────────────────────────────────────────
    if cross_content:
        write_cross_repo_memory(cross_content, date, model_config)

    # ─── 6. Summary output ─────────────────────────────────────────────────────
    print("", file=sys.stderr)
    for repo_path, repo_name, status in summary:
        print(f"  {repo_path}/.mesh/MEMORY.md ({status})")

    if cross_content:
        cross_file = os.path.expanduser("~/.mesh/MEMORY.md")
        print(f"  {cross_file} (written)")

    print("", file=sys.stderr)
    print(f"dream complete — {date}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())