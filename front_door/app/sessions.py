"""In-memory session + CSRF + OAuth-state store (stdlib only).

Keeps the operator's GitHub token **server-side**, keyed by a high-entropy
session id that lives in an httponly cookie — the token never reaches the
browser. This is the cut-1 store; for multi-instance production swap the dict
for Redis/DB (same interface). Sessions and OAuth states expire.

Why server-side instead of a signed cookie holding the token: an OAuth token is
a bearer credential; keeping it out of the browser entirely (httponly cookie
carries only an opaque id) removes a whole class of token-exfiltration risk.
"""

from __future__ import annotations

import secrets
import time
from typing import Optional

SID_COOKIE = "fd_sid"
STATE_COOKIE = "fd_oauth_state"


class SessionStore:
    def __init__(self, *, ttl_seconds: int = 8 * 3600, state_ttl_seconds: int = 600,
                 now=time.time) -> None:
        self._sessions: dict = {}   # sid -> {token, csrf, exp}
        self._states: dict = {}     # state -> exp
        self._ttl = ttl_seconds
        self._state_ttl = state_ttl_seconds
        self._now = now

    # ---- login sessions -----------------------------------------------------
    def create(self, token: str) -> tuple[str, str]:
        """Store a token, return (session_id, csrf_token)."""
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self._sessions[sid] = {"token": token, "csrf": csrf, "exp": self._now() + self._ttl}
        return sid, csrf

    def _live(self, sid: Optional[str]) -> Optional[dict]:
        if not sid:
            return None
        s = self._sessions.get(sid)
        if not s:
            return None
        if s["exp"] < self._now():
            self._sessions.pop(sid, None)
            return None
        return s

    def token_for(self, sid: Optional[str]) -> Optional[str]:
        s = self._live(sid)
        return s["token"] if s else None

    def csrf_for(self, sid: Optional[str]) -> Optional[str]:
        s = self._live(sid)
        return s["csrf"] if s else None

    def delete(self, sid: Optional[str]) -> None:
        if sid:
            self._sessions.pop(sid, None)

    # ---- OAuth handshake state (CSRF for the redirect dance) ----------------
    def new_state(self) -> str:
        st = secrets.token_urlsafe(24)
        self._states[st] = self._now() + self._state_ttl
        return st

    def consume_state(self, state: Optional[str]) -> bool:
        """One-shot check: valid + unexpired. Consumes it so it can't replay."""
        if not state:
            return False
        exp = self._states.pop(state, None)
        return exp is not None and exp >= self._now()


__all__ = ["SessionStore", "SID_COOKIE", "STATE_COOKIE"]
