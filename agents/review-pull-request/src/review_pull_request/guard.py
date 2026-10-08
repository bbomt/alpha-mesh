"""Last line of defence at the model boundary: redact secrets from outgoing messages.

PathPolicy / SafeDiff / PolicyFileReader keep excluded *files* away from the model. This catches
what slips through anyway: literal secrets we know (API and GitHub tokens) and token-shaped strings.
"""

import re

REDACTED = "[REDACTED]"

TOKEN_PATTERNS = [
    r"sk-[A-Za-z0-9_\-]{20,}",            # OpenAI / OpenRouter style keys
    r"gh[pousr]_[A-Za-z0-9]{30,}",        # GitHub tokens
    r"AKIA[0-9A-Z]{16}",                  # AWS access key ids
    r"xox[baprs]-[A-Za-z0-9\-]{10,}",     # Slack tokens
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
]


class Redactor:
    def __init__(self, secrets) -> None:
        literals = sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True)
        parts = [re.escape(s) for s in literals] + TOKEN_PATTERNS
        self._pattern = re.compile("|".join(parts))

    def scrub(self, messages: list[dict]) -> list[dict]:
        """A scrubbed copy; the caller's messages are not modified."""
        out = []
        for message in messages:
            copy = dict(message)
            if isinstance(copy.get("content"), str):
                copy["content"] = self._pattern.sub(REDACTED, copy["content"])
            out.append(copy)
        return out


class GuardedLLM:
    """Decorator: same chat() interface as the wrapped client, with outgoing messages redacted."""

    def __init__(self, inner, redactor: Redactor) -> None:
        self._inner = inner
        self._redactor = redactor

    def chat(self, messages: list[dict], tools=None):
        return self._inner.chat(self._redactor.scrub(messages), tools)
