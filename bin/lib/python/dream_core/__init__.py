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
import base64
import time
from collections import defaultdict, Counter
from typing import Dict, List, Optional, Any, Tuple


# Constants
MESH_SUBDIR = ".mesh"
MEMORY_FILENAME = "MEMORY.md"
CROSS_REPO_TAG = "[CROSS-REPO]"
MAX_MEMORY_LINES = 200
CLAUDE_PROJECTS = os.path.expanduser("~/.claude/projects")


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
        target_repo: Optional repo path to filter results

    Returns:
        Dict mapping repo_path -> {"content": str, "dates": List[str], "file_count": int}
    """
    repo_data = defaultdict(lambda: {"content_lines": [], "dates": set(), "file_count": 0})
    processed = 0

    if not os.path.isdir(projects_dir):
        return {}

    for root, dirs, files in os.walk(projects_dir):
        # Filter to target repo if specified
        if target_repo:
            root_basename = os.path.basename(root)
            if not any(
                candidate in root or root.endswith(candidate)
                for candidate in [target_repo, os.path.basename(target_repo)]
            ):
                continue

        for fname in files:
            if not fname.endswith('.jsonl'):
                continue
            fpath = os.path.join(root, fname)
            processed += 1

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

                # Filter to target repo if specified
                if target_repo and not (target_repo in repo_path or repo_path in target_repo):
                    continue

                repo_data[repo_path]["file_count"] += 1
                session_lines = []
                for msg in messages:
                    session_lines.append(msg)

                if session_lines:
                    content_str = '\n'.join(session_lines)
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


def read_model_config(local_settings_path: str) -> Dict[str, str]:
    """
    Read model configuration from settings.json.

    Args:
        local_settings_path: Path to .claude/settings.json

    Returns:
        Dict with keys: model, base_url, token
    """
    config = {"model": "", "base_url": "", "token": ""}

    if not os.path.isfile(local_settings_path):
        return config

    try:
        with open(local_settings_path) as f:
            settings = json.load(f)

        env = settings.get("env", {})
        config["model"] = env.get("ANTHROPIC_MODEL", "")
        config["base_url"] = env.get("ANTHROPIC_BASE_URL", "")
        config["token"] = env.get("ANTHROPIC_AUTH_TOKEN", env.get("ANTHROPIC_API_KEY", ""))

    except (json.JSONDecodeError, IOError):
        pass

    return config


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

    Handles both Anthropic and OpenAI response formats.

    Args:
        response_text: Raw response text

    Returns:
        Extracted text or empty string
    """
    if not response_text:
        return ""

    # Try parsing as JSON (Anthropic format)
    try:
        r = json.loads(response_text)
        if "content" in r and isinstance(r["content"], list):
            for block in r["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    return block.get("text", "")
        elif "choices" in r:
            return r["choices"][0]["message"].get("content", "")
    except json.JSONDecodeError:
        pass

    # Return as plain text if not JSON
    return response_text.strip()


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
        model_config: Dict from read_model_config
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
                summary.append((repo_name, "written"))
                cross_repo_lines.extend(cross_items)
            else:
                print(f"  Synthesis failed - appending raw learnings", flush=True, file=sys.stderr)
                # Fallback: append
                fallback_content = f"\n\n## {date}\n{new_learnings}"
                if not dry_run:
                    with open(memory_file, "a") as f:
                        f.write(fallback_content)
                summary.append((repo_name, "append-fallback"))
                cross_repo_lines.append(f"## {date} — {repo_name}\n{new_learnings}")

        elif model_config["model"] and model_config["token"]:
            # First run - no existing memory
            print(f"  Creating new memory file...", flush=True, file=sys.stderr)
            new_memory = create_new_memory(new_learnings, date, repo_name)
            if not dry_run:
                with open(memory_file, "w") as f:
                    f.write(new_memory)
            summary.append((repo_name, "new"))

        else:
            # LLM unavailable - raw content fallback
            print(f"  LLM unavailable - writing raw content", flush=True, file=sys.stderr)
            raw_content = f"\n\n## {date} — Raw session content (LLM unavailable)\n{new_learnings}"
            if not dry_run:
                with open(memory_file, "a") as f:
                    f.write(raw_content)
            summary.append((repo_name, "raw-fallback"))

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
    extract_prompt = f"""You are extracting learnings from a software development session.
Be specific and concrete. Do not invent anything not in the source.

## Session content from {repo_name}
{session_content}

Return only a markdown snippet (no preamble) with these sections:

## {date} — {repo_name}
### Architecture
### Deployment
### Testing
### Workflow
### Patterns"""

    base_url = model_config.get("base_url", "") or "https://api.anthropic.com"

    result = call_llm(
        prompt=extract_prompt,
        model=model_config["model"],
        token=model_config["token"],
        base_url=base_url,
        max_tokens=2000
    )

    if result:
        return extract_response_text(result)

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
Produce an updated memory file that:
- Incorporates the new learnings
- Removes duplicates (keep the best single statement of each point)
- Removes anything superseded by newer learnings
- Keeps the file concise — every line must earn its place
- Hard limit: 200 lines

## Existing memory
{existing_memory}

## New learnings
{new_learnings}

Return only the updated memory file content, no preamble.
Keep the same section headings as the existing memory where present.

If any learning applies across multiple repos or to the project as a whole,
prefix it with **[CROSS-REPO]** so it can be collected for the cross-repo memory file."""

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

    sys_prompt = f"""You are maintaining a cross-repo memory file for an AI coding agent.
This file contains learnings that apply across multiple repositories.

## Existing memory
{local_cross_existing or "No existing memory."}

## New cross-repo learnings
{cross_content}

Return only the updated memory file content, no preamble.
Keep the same section headings. Max 200 lines. Deduplicate."""

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
    """Main entry point for dream_core module."""
    import argparse

    parser = argparse.ArgumentParser(description="Dream core logic")
    parser.add_argument("--dry-run", action="store_true", help="Don't write files")
    parser.add_argument("--days", type=int, default=0, help="Days back to process")
    parser.add_argument("--repo", type=str, default="", help="Filter to specific repo")
    parser.add_argument("--projects-dir", type=str, default=CLAUDE_PROJECTS)
    parser.add_argument("--mesh-dir", type=str, required=True)
    parser.add_argument("--date", type=str, default="")
    parser.add_argument("--local-settings", type=str, required=True)

    args = parser.parse_args()

    # Determine cutoff date
    if args.days > 0:
        cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - args.days * 86400))
    else:
        cutoff = time.strftime("%Y-%m-%d")

    date = args.date or time.strftime("%Y-%m-%d")

    # Extract sessions
    repos_data = extract_sessions_from_jsonl(
        projects_dir=args.projects_dir,
        cutoff=cutoff,
        target_repo=args.repo if args.repo else None
    )

    if not repos_data:
        print("No session files found in date range.", file=sys.stderr)
        return 0

    # Read model config
    model_config = read_model_config(args.local_settings)

    # Process repos
    summary, cross_content = process_repos(
        repos_data=repos_data,
        model_config=model_config,
        date=date,
        dry_run=args.dry_run
    )

    # Write cross-repo memory
    if cross_content and not args.dry_run:
        write_cross_repo_memory(cross_content, date, model_config)

    # Print summary
    for repo_name, status in summary:
        print(f"  {repo_name}: .mesh/MEMORY.md ({status})")

    return 0


if __name__ == "__main__":
    sys.exit(main())