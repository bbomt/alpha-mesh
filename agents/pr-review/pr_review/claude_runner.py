"""Claude Code runner for PR review agent."""

import subprocess
import sys
import os
from typing import Optional


class ClaudeRunnerError(Exception):
    """Exception raised for Claude Code runner errors."""
    pass


def run_claude_code_review(
    repo_path: str,
    prompt: str,
    auto_fix: bool = False,
    timeout: int = 300,
    verbose: bool = False,
) -> str:
    """Run Claude Code headlessly in repo_path with the given prompt.

    Args:
        repo_path: Path to the repository where Claude Code should run
        prompt: The review prompt to send to Claude Code
        auto_fix: If True, allow Claude Code to make edits (no --plan-mode)
        timeout: Maximum seconds to wait for Claude Code (default 5 minutes)
        verbose: If True, print additional debug output

    Returns:
        Raw stdout output as a string

    Raises:
        ClaudeRunnerError: On non-zero exit or timeout
    """
    # Build the command.
    # Use --print for headless mode.
    cmd = ["/home/ec2-user/.local/bin/claude", "--print"]

    if verbose:
        print(f"Running Claude Code: {' '.join(cmd)}", file=sys.stderr)
        print(f"CWD: {repo_path}", file=sys.stderr)
        print(f"Prompt length: {len(prompt)} chars", file=sys.stderr)

    # Write prompt to a file in the repo directory (Claude Code has access to it)
    prompt_file = os.path.join(repo_path, ".pr-review-prompt.txt")

    try:
        with open(prompt_file, "w") as f:
            f.write(prompt)

        # Pass prompt file as -p argument
        cmd.extend(["-p", f"Read {prompt_file} and respond to the instructions inside."])

        result = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )

        if verbose:
            print(f"Claude Code stdout ({len(result.stdout)} chars):", file=sys.stderr)
            print(result.stdout[:500] if result.stdout else "(empty)", file=sys.stderr)
            if result.stderr:
                print(f"Claude Code stderr:", file=sys.stderr)
                print(result.stderr[:500], file=sys.stderr)

        if result.returncode != 0:
            error_msg = f"Claude Code exited with code {result.returncode}"
            if result.stderr:
                error_msg += f": {result.stderr}"
            raise ClaudeRunnerError(error_msg)

        return result.stdout

    except subprocess.TimeoutExpired:
        raise ClaudeRunnerError(
            f"Claude Code timed out after {timeout} seconds. "
            "Consider increasing the timeout with a longer --timeout flag."
        )
    except FileNotFoundError:
        raise ClaudeRunnerError(
            "Claude Code CLI not found. Please ensure 'claude' is installed and on your PATH."
        )
    finally:
        # Clean up prompt file
        try:
            os.unlink(prompt_file)
        except Exception:
            pass
