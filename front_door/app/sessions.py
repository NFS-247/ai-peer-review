"""In-memory session + CSRF + OAuth-state store (stdlib only).

Keeps the operator's GitHub token **server-side**, keyed by a high-entropy
session id that lives in an httponly cookie — the token never reaches the
browser. This is the cut-1 store; for multi-instance production swap the dict
for Redis/DB (same interface). Sessions and OAuth states expire.

Why server-side instead of a signed cookie holding the token: an OAuth token is
a bearer credential; keeping it out of the browser entirely (httponly cookie
carries only an opaque id) removes a whole class of token-exfiltration risk.

Memory is bounded: every mutation sweeps expired entries, and hard caps shed the
soonest-to-expire entries if the store is ever flooded (an unauthenticated
attacker can hammer /login, which mints OAuth states — so ``_states`` must not be
allowed to grow without bound).
"""

from __future__ import annotations

import secrets
import time
from typing import Optional

SID_COOKIE = "fd_sid"
STATE_COOKIE = "fd_oauth_state"

# Backstops against unbounded growth. Sweeping handles the normal case; these
# cap the pathological one (a flood of /login hits, or many live sessions).
DEFAULT_MAX_SESSIONS = 5000
DEFAULT_MAX_STATES = 5000


class SessionStore:
    def __init__(self, *, ttl_seconds: int = 8 * 3600, state_ttl_seconds: int = 600,
                 now=time.time, max_sessions: int = DEFAULT_MAX_SESSIONS,
                 max_states: int = DEFAULT_MAX_STATES) -> None:
        self._sessions: dict = {}   # sid -> {token, csrf, exp}
        self._states: dict = {}     # state -> exp
        self._ttl = ttl_seconds
        self._state_ttl = state_ttl_seconds
        self._now = now
        self._max_sessions = max_sessions
        self._max_states = max_states

    # ---- bounding -----------------------------------------------------------
    def _sweep(self) -> None:
        """Drop every expired session and state. Called on each mutation so the
        in-memory store cannot grow without bound under normal use."""
        now = self._now()
        self._sessions = {k: v for k, v in self._sessions.items() if v["exp"] >= now}
        self._states = {k: exp for k, exp in self._states.items() if exp >= now}

    @staticmethod
    def _evict_oldest(store: dict, exp_of, keep: int) -> None:
        """Shed soonest-to-expire entries so ``len(store) <= keep`` (cap backstop)."""
        excess = len(store) - keep
        if excess > 0:
            for k in sorted(store, key=exp_of)[:excess]:
                store.pop(k, None)

    # ---- login sessions -----------------------------------------------------
    def create(self, token: str) -> tuple[str, str]:
        """Store a token, return (session_id, csrf_token)."""
        self._sweep()
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        self._sessions[sid] = {"token": token, "csrf": csrf, "exp": self._now() + self._ttl}
        self._evict_oldest(self._sessions, lambda k: self._sessions[k]["exp"], self._max_sessions)
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
        self._sweep()
        st = secrets.token_urlsafe(24)
        self._states[st] = self._now() + self._state_ttl
        self._evict_oldest(self._states, self._states.get, self._max_states)
        return st

    def consume_state(self, state: Optional[str]) -> bool:
        """One-shot check: valid + unexpired. Consumes it so it can't replay.

        Note: this proves the state was issued by us and is live, but NOT that the
        callback came from the browser that started the login. The router binds
        that separately via the ``fd_oauth_state`` cookie set at /login.
        """
        if not state:
            return False
        exp = self._states.pop(state, None)
        return exp is not None and exp >= self._now()


__all__ = ["SessionStore", "SID_COOKIE", "STATE_COOKIE",
           "DEFAULT_MAX_SESSIONS", "DEFAULT_MAX_STATES"]
