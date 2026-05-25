"""CLI entry point for dream agent.

This provides a Python CLI entry point for the dream memory consolidation agent,
similar to how plugin_cli.py provides a CLI for the pr-review agent.
"""

import argparse
import os
import sys
import time
from pathlib import Path

# Import the dream module
from dream import (
    extract_sessions_from_jsonl,
    synthesize_memory,
    create_new_memory,
    split_routing,
    enforce_recent_signals_cap,
    check_conflicts,
)
from dream import (
    MESH_SUBDIR,
    MEMORY_FILENAME,
    MAX_MEMORY_LINES,
    CLAUDE_PROJECTS,
    LAYER1_HEADER,
    LAYER2_HEADER,
    LAYER3_HEADER,
    CONFLICTS_HEADER,
)


LOG_DIR = Path.home() / "environment" / "logs" / "ai" / "claude"
LOG_FILE = LOG_DIR / f"alpha-mesh-{time.strftime('%Y-%m-%d')}.log"


def log_command(cmd: str, output: str = "", error: str = "", verbose: bool = False) -> None:
    """Log a command and its output to the log file."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(f"\n$ {cmd}\n")
            if output:
                f.write(f"OUTPUT:\n{output}\n")
            if error:
                f.write(f"ERROR:\n{error}\n")
    except Exception as e:
        print(f"Warning: log_command failed: {e}", file=sys.stderr)

    if verbose:
        print(f"$ {cmd}", file=sys.stderr)
        if output:
            print(output[:500] + "..." if len(output) > 500 else output, file=sys.stderr)
        if error:
            print(f"ERROR: {error}", file=sys.stderr)


def main() -> int:
    """Main entry point for dream CLI."""
    parser = argparse.ArgumentParser(
        description="Dream Agent - End-of-session memory consolidation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  dream --dry-run
  dream --days 3
  dream --repo ~/environment/pmat
  dream --include-assistant
"""
    )
    parser.add_argument(
        "--projects-dir",
        type=str,
        default=CLAUDE_PROJECTS,
        help=f"Directory containing Claude projects (default: {CLAUDE_PROJECTS})",
    )
    parser.add_argument(
        "--model",
        type=str,
        required=True,
        help="Model name for LLM calls",
    )
    parser.add_argument(
        "--token",
        type=str,
        required=True,
        help="API token for LLM calls",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        required=True,
        help="Base URL for LLM API",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print extracted content, write nothing",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Number of days to look back (default: 1)",
    )
    parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Process only specific repo",
    )
    parser.add_argument(
        "--include-assistant",
        action="store_true",
        help="Include assistant messages (default: user only)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print detailed logs to stderr",
    )

    args = parser.parse_args()
    verbose = args.verbose

    log_command(f"dream --days {args.days} --repo {args.repo} --dry-run={args.dry_run}", verbose=verbose)

    # Extract sessions
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")

    print(f"Extracting sessions since {cutoff}...", file=sys.stderr)

    sessions = extract_sessions_from_jsonl(
        projects_dir=args.projects_dir,
        cutoff=cutoff,
        target_repo=args.repo,
        include_assistant=args.include_assistant,
    )

    if not sessions:
        print("No sessions found.", file=sys.stderr)
        return 0

    print(f"Found {len(sessions)} repos with sessions", file=sys.stderr)

    model_config = {
        "model": args.model,
        "token": args.token,
        "base_url": args.base_url,
    }

    # Process each repo
    for repo_path, session_data in sessions.items():
        repo_name = os.path.basename(repo_path)
        print(f"\nProcessing {repo_name}...", file=sys.stderr)

        content = session_data.get("content", "")
        dates = session_data.get("dates", [])

        if args.dry_run:
            print("=" * 60, file=sys.stderr)
            print(f"DRY RUN - {repo_name}", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            print(content[:1000] + "..." if len(content) > 1000 else content, file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            continue

        # Read existing memory if present
        memory_path = Path(repo_path) / MESH_SUBDIR / MEMORY_FILENAME
        existing_memory = ""
        if memory_path.exists():
            existing_memory = memory_path.read_text()

        # Synthesize new memory
        new_memory = synthesize_memory(
            existing_memory=existing_memory,
            new_learnings=content,
            date=datetime.now().strftime("%Y-%m-%d"),
            repo_name=repo_name,
            model_config=model_config,
        )

        if not new_memory:
            print(f"Failed to synthesize memory for {repo_name}", file=sys.stderr)
            continue

        # Split routing
        repo_local, global_md = split_routing(new_memory)

        # Enforce signals cap
        repo_local = enforce_recent_signals_cap(repo_local, max_entries=5)
        global_md = enforce_recent_signals_cap(global_md, max_entries=5)

        # Check conflicts
        has_conflicts, conflicts = check_conflicts(repo_local)

        # Write memories
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(repo_local)
        print(f"Updated {memory_path}", file=sys.stderr)

        # TODO: Handle global memory routing if needed

    print("\nDream complete!", file=sys.stderr)
    log_command("dream completed successfully", verbose=verbose)
    return 0


if __name__ == "__main__":
    sys.exit(main())
