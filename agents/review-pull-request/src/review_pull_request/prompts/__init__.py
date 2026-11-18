"""The review's prompts, one file each, so a prompt change is its own diff (PMAT-312)."""

from pathlib import Path


def load_prompt(name: str) -> str:
    """The text of prompts/<name>, a str.format template."""
    return (Path(__file__).parent / name).read_text(encoding="utf-8")
