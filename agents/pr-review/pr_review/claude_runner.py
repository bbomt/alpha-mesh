"""Claude Code runner for PR review agent."""

import shutil
import subprocess
import sys
import os
import json
import tempfile
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
        verbose: If True, print Claude Code output to stderr for visibility

    Returns:
        Raw stdout output as a string

    Raises:
        ClaudeRunnerError: On non-zero exit or timeout
    """
    # Build the command.
    # Use --print for headless mode (non-interactive output).
    # Use --output-format json for structured output (clean, parseable).
    cmd = [shutil.which("claude") or "/home/ec2-user/.local/bin/claude", "--print", "--output-format", "json"]

    if verbose:
        print(f"Running Claude Code: {' '.join(cmd)}", file=sys.stderr)
        print(f"CWD: {repo_path}", file=sys.stderr)
        print(f"Prompt length: {len(prompt)} chars", file=sys.stderr)
        print("Streaming Claude Code output to stderr (verbose mode)...", file=sys.stderr)
        print("-" * 60, file=sys.stderr)

    try:
        # Write prompt to temp file and pipe via stdin to avoid argument length limits
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write(prompt)
            prompt_file = f.name

        try:
            with open(prompt_file) as pf:
                result = subprocess.run(
                    cmd,
                    cwd=repo_path,
                    stdin=pf,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
        finally:
            os.unlink(prompt_file)

        if verbose:
            if result.stderr:
                print(result.stderr, file=sys.stderr)
            print("-" * 60, file=sys.stderr)
            print(f"Claude Code exit code: {result.returncode}", file=sys.stderr)

        if result.returncode != 0:
            error_msg = f"Claude Code exited with code {result.returncode}"
            if result.stderr:
                error_msg += f": {result.stderr}"
            raise ClaudeRunnerError(error_msg)

        # Parse JSON output to extract the result field
        try:
            data = json.loads(result.stdout)
            output = data.get("result", "")

            if verbose:
                print(f"Parsed JSON result ({len(output)} chars):", file=sys.stderr)
                print(output[:500] + "..." if len(output) > 500 else output, file=sys.stderr)

            return output
        except json.JSONDecodeError as e:
            if verbose:
                print(f"Failed to parse JSON output: {e}", file=sys.stderr)
                print(f"Raw stdout: {result.stdout[:1000]}", file=sys.stderr)
            # Fall back to raw output
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
