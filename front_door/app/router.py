"""Request routing: ties gh + viewmodels + render together.

``route()`` is pure-ish — all I/O goes through an injected ``Deps`` (read client,
operator-client factory, session store, OAuth, config, clock) — so the board,
inbox, the OAuth handshake, and the action POST are all unit-testable with fakes
and no network. ``server.py`` is the only part that touches sockets.

Auth: the GitHub token lives server-side in the session store, keyed by an opaque
httponly ``fd_sid`` cookie. Writes (Approve/Block/Investigate) are posted with
that token so the engine accepts them as the operator, and are CSRF-protected
when a session is present. With no OAuth configured, the dev operator token is
used and CSRF is skipped (local single-user dev).
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import render
from .commands import VALID_ACTIONS, command_body
from .sessions import SID_COOKIE
from .viewmodels import (
    GLOBAL_SPEND_MARKER,
    ProjectSpend,
    build_board_row,
    inbox_from_rows,
    spend_from_ledger,
)


@dataclass
class Response:
    status: int
    body: str
    content_type: str = "text/html; charset=utf-8"
    headers: dict = field(default_factory=dict)


@dataclass
class Deps:
    read: object                       # board reads (a gh.GitHub or a fake)
    operator_client: Callable          # (cookies: dict) -> client | None  (writes)
    cfg: object                        # config.Config
    now_ts: float
    sessions: object = None            # SessionStore (None -> dev/no-session mode)
    oauth: object = None               # OAuth (None/unconfigured -> dev mode)


# ---- cookies ----------------------------------------------------------------
def _secure(cfg) -> bool:
    return getattr(cfg, "public_base_url", "").startswith("https")


def _set_cookie(name: str, value: str, *, max_age: int, secure: bool) -> str:
    parts = [f"{name}={value}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _clear_cookie(name: str, *, secure: bool) -> str:
    parts = [f"{name}=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def _sid(cookies: dict) -> Optional[str]:
    return cookies.get(SID_COOKIE)


def _session_csrf(deps: Deps, cookies: dict) -> Optional[str]:
    return deps.sessions.csrf_for(_sid(cookies)) if deps.sessions else None


def _signed_in(deps: Deps, cookies: dict) -> bool:
    return bool(deps.sessions and deps.sessions.token_for(_sid(cookies)))


# ---- board/inbox assembly ---------------------------------------------------
def _gather(deps: Deps):
    """[(repo, [BoardRow], ProjectSpend)] across all configured repos."""
    grouped = []
    for repo in deps.cfg.repos:
        rows = []
        for pr in deps.read.list_open_pulls(repo):
            n = int(pr.get("number", 0))
            labels = deps.read.list_labels(repo, n)
            comments = deps.read.list_issue_comments(repo, n)
            rows.append(build_board_row(repo=repo, pr=pr, labels=labels, comments=comments))
        ledger = deps.read.find_issue_body_by_marker(repo, GLOBAL_SPEND_MARKER)
        total, by = spend_from_ledger(ledger, now_ts=deps.now_ts)
        grouped.append((repo, rows, ProjectSpend(repo=repo, total_24h_usd=total, by_provider=by)))
    return grouped


def _all_rows(grouped) -> list:
    out = []
    for _repo, rows, _spend in grouped:
        out.extend(rows)
    return out


# ---- routes -----------------------------------------------------------------
def route(method: str, path: str, *, cookies: dict, form: dict, deps: Deps,
          query: Optional[dict] = None) -> Response:
    query = query or {}

    if method == "GET" and path == "/healthz":
        return Response(200, "ok", "text/plain; charset=utf-8")

    if method == "GET" and path == "/":
        return Response(200, render.board_page(_gather(deps), signed_in=_signed_in(deps, cookies)))

    if method == "GET" and path == "/inbox":
        items = inbox_from_rows(_all_rows(_gather(deps)))
        return Response(200, render.inbox_page(
            items, signed_in=_signed_in(deps, cookies), csrf=_session_csrf(deps, cookies) or ""))

    if method == "GET" and path == "/login":
        return _handle_login(deps)

    if method == "GET" and path == "/auth/callback":
        return _handle_callback(deps=deps, query=query)

    if method == "GET" and path == "/logout":
        if deps.sessions:
            deps.sessions.delete(_sid(cookies))
        return Response(303, "", headers={
            "Location": "/", "Set-Cookie": _clear_cookie(SID_COOKIE, secure=_secure(deps.cfg))})

    if method == "POST" and path == "/action":
        return _handle_action(cookies=cookies, form=form, deps=deps)

    return Response(404, render.message_page("Not found", "No such page."))


def _handle_login(deps: Deps) -> Response:
    oauth = deps.oauth
    if not (oauth and oauth.configured() and deps.sessions):
        return Response(200, render.message_page(
            "Sign in",
            "OAuth isn't configured. Writes use <code>FRONT_DOOR_DEV_TOKEN</code> "
            "for local dev; set <code>GITHUB_OAUTH_CLIENT_ID/SECRET</code> + "
            "<code>FRONT_DOOR_PUBLIC_URL</code> to enable GitHub sign-in."))
    state = deps.sessions.new_state()
    url = oauth.authorize_url(state=state, redirect_uri=deps.cfg.redirect_uri())
    return Response(303, "", headers={"Location": url})


def _handle_callback(*, deps: Deps, query: dict) -> Response:
    if not (deps.oauth and deps.sessions):
        return Response(400, render.message_page("Sign in", "OAuth not enabled."))
    if not deps.sessions.consume_state(query.get("state")):
        # Forged/expired/replayed state -> reject (CSRF on the handshake).
        return Response(400, render.message_page("Sign in failed", "Invalid or expired state."))
    code = (query.get("code") or "").strip()
    if not code:
        return Response(400, render.message_page("Sign in failed", "Missing code."))
    try:
        token = deps.oauth.exchange_code(code=code, redirect_uri=deps.cfg.redirect_uri())
    except Exception as exc:  # noqa: BLE001
        return Response(502, render.message_page(
            "Sign in failed", f"Token exchange failed: {type(exc).__name__}."))
    sid, _csrf = deps.sessions.create(token)
    return Response(303, "", headers={
        "Location": "/",
        "Set-Cookie": _set_cookie(SID_COOKIE, sid, max_age=8 * 3600, secure=_secure(deps.cfg)),
    })


def _csrf_ok(deps: Deps, cookies: dict, form: dict) -> bool:
    """Valid iff there is no session layer/session (dev mode) OR the form's csrf
    matches the session's. Prevents a cross-site POST from acting as the user."""
    expected = _session_csrf(deps, cookies)
    if expected is None:
        return True  # dev/no-session mode — nothing to bind a token to
    return secrets.compare_digest(form.get("csrf", ""), expected)


def _handle_action(*, cookies: dict, form: dict, deps: Deps) -> Response:
    repo = (form.get("repo") or "").strip()
    action = (form.get("action") or "").strip()
    text = form.get("text") or ""
    try:
        number = int(form.get("number") or "0")
    except ValueError:
        number = 0

    # Only ever act on a configured repo + a known action + a real PR number.
    if repo not in getattr(deps.cfg, "repos", ()):  # never post to an arbitrary repo
        return Response(400, render.message_page("Bad request", "Unknown repository."))
    if action not in VALID_ACTIONS or number <= 0:
        return Response(400, render.message_page("Bad request", "Invalid action."))

    if not _csrf_ok(deps, cookies, form):
        return Response(403, render.message_page("Blocked", "Invalid CSRF token; reload and retry."))

    client = deps.operator_client(cookies)
    if client is None:
        return Response(401, render.message_page(
            "Sign in required",
            "No operator identity. <a href='/login'>Sign in.</a>"))

    try:
        body = command_body(action, text)
    except ValueError as exc:
        return Response(400, render.message_page("Bad request", f"{exc}"))

    try:
        client.post_issue_comment(repo, number, body)
    except Exception as exc:  # noqa: BLE001 - surface the failure, never 500 silently
        return Response(502, render.message_page(
            "Could not post", f"GitHub rejected the command: {type(exc).__name__}."))

    # Posted as the operator; the engine reacts. Back to the queue.
    return Response(303, "", headers={"Location": "/inbox"})


__all__ = ["Response", "Deps", "route"]
