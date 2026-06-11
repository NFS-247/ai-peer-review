"""Operator decisions -> the exact PR comment the engine acts on.

The engine recognizes a command only when the comment's FIRST line is
``OPERATOR <VERB>`` and it is authored by the repo's configured operator. This
module builds those bodies and the optional signed one-tap approve/merge URL
(same HMAC scheme as the engine's chat-approve web app). Pure.
"""

from __future__ import annotations

import hashlib
import hmac
import urllib.parse


OPERATOR_PREFIX = "OPERATOR "

ACTION_APPROVE = "approve"
ACTION_BLOCK = "block"
ACTION_INVESTIGATE = "investigate"
# Map a UI action to the engine verb. Approve/merge clicks that go through the
# signed web-app URL use approve_url() instead of a posted comment.
_VERB = {ACTION_APPROVE: "APPROVE", ACTION_BLOCK: "BLOCK", ACTION_INVESTIGATE: "INVESTIGATE"}
VALID_ACTIONS = frozenset(_VERB)

# Actions that require operator-supplied text (a reason / a note).
_NEEDS_TEXT = frozenset({ACTION_BLOCK, ACTION_INVESTIGATE})


def command_body(action: str, text: str = "") -> str:
    """The exact comment body for a UI action. Raises ValueError if invalid.

    ``text`` is required for block (reason) and investigate (note); ignored for
    approve. Newlines in ``text`` are flattened so the command stays on the
    first line, which is what the engine parses.
    """
    if action not in VALID_ACTIONS:
        raise ValueError(f"unknown action {action!r}")
    cleaned = " ".join((text or "").split())
    if action in _NEEDS_TEXT and not cleaned:
        raise ValueError(f"action {action!r} requires text")
    verb = _VERB[action]
    return f"{OPERATOR_PREFIX}{verb} {cleaned}".rstrip()


def sign_action(secret: str, *, repo: str, pr_number: int, action: str) -> str:
    """HMAC-SHA256 over ``repo:pr:action`` (same as the engine's chat-approve)."""
    msg = f"{repo}:{pr_number}:{action}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def approve_url(
    base_url: str, *, repo: str, pr_number: int, action: str = "approve", signing_secret: str = ""
) -> str:
    """Signed one-tap approve/approve_merge link (returns '' without a base)."""
    if not base_url:
        return ""
    params = {"repo": repo, "pr": pr_number, "action": action}
    if signing_secret:
        params["sig"] = sign_action(signing_secret, repo=repo, pr_number=pr_number, action=action)
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}{urllib.parse.urlencode(params)}"


__all__ = [
    "OPERATOR_PREFIX", "ACTION_APPROVE", "ACTION_BLOCK", "ACTION_INVESTIGATE",
    "VALID_ACTIONS", "command_body", "sign_action", "approve_url",
]
