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


# Constants
MESH_SUBDIR = ".mesh"
MEMORY_FILENAME = "MEMORY.md"
CROSS_REPO_TAG = "[CROSS-REPO]"
MAX_MEMORY_LINES = 200
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")
SECTION_FIELDS = ["architecture", "patterns", "jira_workflow",
                  "deploy_commands", "testing", "workflow"]


def extract_sessions_from_jsonl(
    projects_dir: str,
    cutoff: str,
    target_repo: Optional[str] = None
) -> Dict[str, Dict[str, Any]]:
    """
    Scan JSONL session files and extract content by repo.

    Args:
        projects_dir: Path to ~/.claude/projects
        cutoff: Date string (YYYY-MM-DD) - only process files modified since
        target_repo: Optional repo path to filter results.
            When set to a parent directory (e.g., ~/environment/pmat),
            all child repos are included so multi-repo work is captured.

    Returns:
        Dict mapping repo_path -> {"content": str, "dates": List[str], "file_count": int}
    """
    repo_data = defaultdict(lambda: {"content_lines": [], "dates": set(), "file_count": 0})

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

            try:
                with open(fpath) as f:
                    for line in f:
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

                        msg = obj.get('message', {})
                        if 'role' not in msg:
                            msg = obj
                        role = msg.get('role', '')
                        content = msg.get('content', '')

                        # Only process valid assistant and user messages
                        if role == 'assistant' and isinstance(content, list):
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
                            if text and len(text) > 20 and not text.startswith('/'):
                                messages.append(f"[user] {text[:300]}")

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

            except OSError:
                continue

    result = {}
    for repo_path, data in repo_data.items():
        if data["content_lines"]:
            result[repo_path] = {
                "content": '\n\n'.join(data["content_lines"]),
                "dates": sorted(data["dates"]),
                "file_count": data["file_count"]
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

        # Fix MiniMax URL: https://api.minimax.io/anthropic -> https://api.minimax.io/v1
        adjusted_base_url = base_url
        if base_url and base_url.rstrip("/").endswith("/anthropic"):
            adjusted_base_url = base_url.rstrip("/").replace("/anthropic", "") + "/v1"

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
    total_repos = len(repos_data)
    current_repo = 0

    for repo_path, info in sorted(repos_data.items()):
        current_repo += 1
        repo_name = os.path.basename(repo_path)
        dates_str = ', '.join(info['dates'])

        print(f"[{current_repo}/{total_repos}] Processing: {repo_name} ({dates_str})", flush=True)

        # Step A: Extract learnings
        print(f"  Extracting learnings...", flush=True, file=sys.stderr)
        session_content = info["content"]
        new_learnings = extract_learnings(
            session_content=session_content,
            repo_name=repo_name,
            date=date,
            model_config=model_config
        )

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
        if model_config["model"] and model_config["token"] and existing_memory:
            print(f"  Synthesizing with existing memory...", flush=True, file=sys.stderr)
            updated_memory, cross_items = synthesize_memory(
                existing_memory=existing_memory,
                new_learnings=new_learnings,
                date=date,
                repo_name=repo_name,
                model_config=model_config
            )

            if updated_memory:
                if not dry_run:
                    with open(memory_file, "w") as f:
                        f.write(updated_memory)
                summary.append((repo_path, repo_name, "written"))
                cross_repo_lines.extend(cross_items)
            else:
                print(f"  Synthesis failed - appending raw learnings", flush=True, file=sys.stderr)
                # Fallback: append
                fallback_content = f"\n\n## {date}\n{new_learnings}"
                if not dry_run:
                    with open(memory_file, "a") as f:
                        f.write(fallback_content)
                summary.append((repo_path, repo_name, "append-fallback"))
                cross_repo_lines.append(f"## {date} — {repo_name}\n{new_learnings}")

        elif model_config["model"] and model_config["token"]:
            # First run - no existing memory
            print(f"  Creating new memory file...", flush=True, file=sys.stderr)
            new_memory = create_new_memory(new_learnings, date, repo_name)
            if not dry_run:
                with open(memory_file, "w") as f:
                    f.write(new_memory)
            summary.append((repo_path, repo_name, "new"))
            # Extract cross-repo items from new learnings
            for line in new_learnings.split('\n'):
                if CROSS_REPO_TAG in line:
                    cross_repo_lines.append(line)

        else:
            # LLM unavailable - raw content fallback
            print(f"  LLM unavailable - writing raw content", flush=True, file=sys.stderr)
            raw_content = f"\n\n## {date} — Raw session content (LLM unavailable)\n{new_learnings}"
            if not dry_run:
                with open(memory_file, "a") as f:
                    f.write(raw_content)
            summary.append((repo_path, repo_name, "raw-fallback"))

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
    extract_prompt = f"""Extract learnings from this AI coding session. Output ONLY valid markdown — no JSON, no code fences, no explanatory text.

Required format:
### Architecture
- [one-line architectural decision]

### Patterns
- [one-line code pattern or technique]

### Jira Workflow
- [one-line Jira or process learning]

### Deploy Commands
- [one-line deployment command or script]

### Testing
- [one-line testing approach or command]

### Workflow
- [one-line development workflow learning]

If no real learning exists for a section, omit the section entirely. Mark cross-repo learnings with **[CROSS-REPO]**.

## Session content
{session_content}"""

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
) -> Tuple[Optional[str], List[str]]:
    """
    Synthesize existing memory with new learnings.

    Args:
        existing_memory: Current memory file content
        new_learnings: New learnings to merge
        date: Current date
        repo_name: Name of the repo
        model_config: Model configuration dict

    Returns:
        Tuple of (updated_memory, cross_repo_items)
    """
    sys_prompt = f"""You are maintaining a memory file for an AI coding agent.
Below is the existing memory and new learnings from recent sessions.

Return a JSON object with this structure:
{{
  "content": "updated memory file content as markdown..."
}}

The content must:
- Incorporate the new learnings
- Remove duplicates (keep the best single statement of each point)
- Remove anything superseded by newer learnings
- Keep the file concise — every line must earn its place
- Hard limit: 200 lines

## Existing memory
{existing_memory}

## New learnings
{new_learnings}

If any learning applies across multiple repos or to the project as a whole,
prefix it with **[CROSS-REPO]** in the content field."""

    base_url = model_config.get("base_url", "") or "https://api.anthropic.com"

    result = call_llm(
        prompt=sys_prompt,
        model=model_config["model"],
        token=model_config["token"],
        base_url=base_url,
        max_tokens=3000
    )

    if not result:
        return None, []

    updated_memory = extract_response_text(result)
    if not updated_memory:
        return None, []

    # Extract cross-repo items
    cross_items = []
    for line in updated_memory.split('\n'):
        if CROSS_REPO_TAG in line:
            cross_items.append(line)

    return updated_memory, cross_items


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

    # Strip [CROSS-REPO] markers — global memory is already cross-repo by nature
    clean_content = re.sub(r'\*\*\[CROSS-REPO\]\*\*', '', cross_content)

    sys_prompt = f"""You are maintaining a cross-repo memory file for an AI coding agent.
This file contains learnings that apply across multiple repositories.

Return a JSON object with this structure:
{{
  "content": "updated memory file content as markdown..."
}}

The content must:
- Deduplicate with existing memory
- Keep concise (max 200 lines)
- Use same section headings as existing

## Existing memory
{local_cross_existing or "No existing memory."}

## New cross-repo learnings
{clean_content}"""

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
        target_repo=args.repo if args.repo else None
    )

    file_count = sum(info.get('file_count', 0) for info in repos_data.values())
    if file_count == 0:
        print(f"No session files found modified since {cutoff}.", file=sys.stderr)
        print("dream complete — nothing to process.", file=sys.stderr)
        return 0

    print(f"Found {file_count} session file(s)", file=sys.stderr)

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

    base_url = args.base_url or "https://api.minimax.io/v1"

    model_config = {
        "model": args.model,
        "token": args.token,
        "base_url": base_url
    }

    print(f"Using model: {args.model}  |  API: {base_url}", file=sys.stderr)

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