"""Secret redaction for anything the dispatcher posts publicly.

The dispatcher posts failure/escalation comments that include exception text.
An exception from an HTTP client can contain the Authorization header, which
contains an API key. This module scrubs anything key-shaped before it is ever
written to a PR comment or log.

Two layers:
1. Pattern-based: redact strings that look like known key formats
   (sk-ant-..., sk-proj-..., sk-..., Bearer <token>, AIza..., re_...).
2. Value-based: redact exact known secret values registered at runtime.

Layer 2 is the strong guarantee — the dispatcher registers its actual secret
values, so even an unrecognized key format is scrubbed if it matches a real
secret. Layer 1 is defense in depth for formats we did not register.
"""

from __future__ import annotations

import re

REDACTED = "[REDACTED]"

# Known key-shaped patterns. Conservative: each requires a long token body so
# ordinary text is not mangled.
_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-proj-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),       # Google API keys
    re.compile(r"re_[A-Za-z0-9_\-]{20,}"),         # Resend keys
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),           # GitHub PATs
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    # "Bearer <token>" — redact the token but keep the word Bearer.
    re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{20,}"),
]

# Exact secret values registered at runtime (layer 2).
_REGISTERED: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register an exact secret value to be scrubbed from all output."""
    if value and len(value) >= 8:
        _REGISTERED.add(value)
        # Also register a stripped form in case of trailing whitespace.
        _REGISTERED.add(value.strip())


def clear_registered() -> None:
    """Test helper: forget all registered secrets."""
    _REGISTERED.clear()


def redact(text: str) -> str:
    """Return ``text`` with all known/likely secrets replaced by [REDACTED]."""
    if not text:
        return text
    out = text
    # Layer 2 first: exact registered values (longest first so substrings of a
    # longer secret don't leave fragments).
    for value in sorted(_REGISTERED, key=len, reverse=True):
        if value:
            out = out.replace(value, REDACTED)
    # Layer 1: pattern-based.
    for pat in _PATTERNS:
        if pat.groups:
            out = pat.sub(lambda m: m.group(1) + REDACTED, out)
        else:
            out = pat.sub(REDACTED, out)
    return out


__all__ = ["REDACTED", "register_secret", "clear_registered", "redact"]
