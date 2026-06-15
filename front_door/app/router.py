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
import sys
from dataclasses import dataclass, field
from html import escape
from typing import Callable, Optional

from . import render
from .commands import VALID_ACTIONS, command_body
from .gh import GitHubError
from .provision import PROVIDER_ORDER, provision
from .sessions import SID_COOKIE, STATE_COOKIE
from .viewmodels import (
    GLOBAL_SPEND_MARKER,
    ProjectSpend,
    RepoView,
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
    provisioning_client: Callable = None  # (cookies) -> ProvisioningClient | None


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
def _issue_number(comment: dict) -> int:
    """PR/issue number a repo-wide comment belongs to, from its ``issue_url``
    (e.g. .../issues/114). 0 if it can't be parsed."""
    tail = (comment.get("issue_url") or "").rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _group_comments_by_pr(comments: list) -> dict:
    grouped: dict = {}
    for c in comments:
        n = _issue_number(c)
        if n:
            grouped.setdefault(n, []).append(c)
    return grouped


def _gather(deps: Deps) -> "list[RepoView]":
    """One RepoView per configured repo. A repo that fails to read becomes a
    RepoView with an ``error`` (and no rows) so the rest of the board still
    renders — a bad token or a single unreachable repo can't 500 the page."""
    views = []
    for repo in deps.cfg.repos:
        try:
            rows = []
            # One repo-wide comment sweep, grouped by PR — not a call per PR.
            comments_by_pr = _group_comments_by_pr(
                deps.read.list_issue_comments_for_repo(repo))
            for pr in deps.read.list_open_pulls(repo):
                n = int(pr.get("number", 0))
                # Labels come back on the pulls payload itself — no extra per-PR
                # request (the list endpoint already includes them).
                labels = [l.get("name", "") for l in (pr.get("labels") or [])
                          if isinstance(l, dict)]
                comments = comments_by_pr.get(n, [])
                rows.append(build_board_row(repo=repo, pr=pr, labels=labels, comments=comments))
            ledger = deps.read.find_issue_body_by_marker(repo, GLOBAL_SPEND_MARKER)
            total, by = spend_from_ledger(ledger, now_ts=deps.now_ts)
            views.append(RepoView(repo, rows, ProjectSpend(repo, total, by)))
        except Exception as exc:  # noqa: BLE001 - isolate per-repo failures
            # Log the detail server-side; show the user a generic message so a
            # GitHub error body (which could echo request data) never reaches
            # the page.
            print(f"front_door: failed to read {repo}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            views.append(RepoView(repo, [], ProjectSpend(repo, 0.0, {}),
                                  error="could not be read (see server logs)"))
    return views


def _all_rows(views) -> list:
    out = []
    for v in views:
        out.extend(v.rows)
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
        return _handle_callback(deps=deps, cookies=cookies, query=query)

    if method == "GET" and path == "/logout":
        if deps.sessions:
            deps.sessions.delete(_sid(cookies))
        return Response(303, "", headers={
            "Location": "/", "Set-Cookie": _clear_cookie(SID_COOKIE, secure=_secure(deps.cfg))})

    if method == "POST" and path == "/action":
        return _handle_action(cookies=cookies, form=form, deps=deps)

    if method == "GET" and path == "/connect":
        return _handle_connect_form(deps=deps, cookies=cookies)

    if method == "POST" and path == "/connect":
        return _handle_connect_submit(deps=deps, cookies=cookies, form=form)

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
    # Bind the state to THIS browser via an httponly cookie; the callback must
    # present a matching cookie. Without this, a globally-valid state lets an
    # attacker complete the callback in a victim's browser (login-CSRF).
    return Response(303, "", headers={
        "Location": url,
        "Set-Cookie": _set_cookie(STATE_COOKIE, state, max_age=600, secure=_secure(deps.cfg)),
    })


def _handle_callback(*, deps: Deps, cookies: dict, query: dict) -> Response:
    if not (deps.oauth and deps.sessions):
        return Response(400, render.message_page("Sign in", "OAuth not enabled."))
    state = query.get("state") or ""
    cookie_state = cookies.get(STATE_COOKIE) or ""
    # Browser-binding: the state in the URL must match the state cookie set at
    # /login AND be a live one-shot server-side state. The cookie match defeats
    # login-CSRF / state-fixation (an attacker's state lives in the attacker's
    # cookie, not the victim's); consume_state defeats replay/forgery.
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        return Response(400, render.message_page("Sign in failed", "Invalid or expired state."))
    if not deps.sessions.consume_state(state):
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
    # The state cookie is now spent server-side; it is overwritten on the next
    # /login and harmless until its short Max-Age lapses.
    return Response(303, "", headers={
        "Location": "/",
        "Set-Cookie": _set_cookie(SID_COOKIE, sid, max_age=8 * 3600, secure=_secure(deps.cfg)),
    })


def _csrf_ok(deps: Deps, cookies: dict, form: dict) -> bool:
    """CSRF policy for writes.

    Dev mode (no session layer at all): skip — there is no session to bind a
    token to, and writes use the local dev token only. OAuth mode (session layer
    present): ALWAYS require a valid CSRF token that matches the caller's
    session. A request with no session must fail closed here — otherwise "no
    session" would be a CSRF bypass, which combined with any operator client is a
    cross-site write hole.
    """
    if deps.sessions is None:
        return True  # dev/read-only mode — no session layer
    expected = _session_csrf(deps, cookies)
    if not expected:
        return False  # session layer exists but caller has no session -> reject
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


# ---- connect (self-serve provisioning) --------------------------------------
def _provisioner(deps: Deps, cookies: dict):
    """The operator's provisioning client (their own token), or None if no
    identity — provisioning always runs AS the signed-in user, on their repos."""
    factory = getattr(deps, "provisioning_client", None)
    return factory(cookies) if factory else None


def _handle_connect_form(*, deps: Deps, cookies: dict, error: str = "") -> Response:
    client = _provisioner(deps, cookies)
    if client is None:
        return Response(200, render.message_page(
            "Connect a repo",
            "Sign in with GitHub to connect a repository — provisioning runs as "
            "you, on your own repos. <a href='/login'>Sign in.</a>",
            signed_in=_signed_in(deps, cookies)))
    return Response(200, render.connect_page(
        login=client.login, csrf=_session_csrf(deps, cookies) or "",
        signed_in=_signed_in(deps, cookies), error=error))


def _handle_connect_submit(*, deps: Deps, cookies: dict, form: dict) -> Response:
    client = _provisioner(deps, cookies)
    if client is None:
        return Response(401, render.message_page(
            "Sign in required",
            "Sign in with GitHub to connect a repository. <a href='/login'>Sign in.</a>",
            signed_in=_signed_in(deps, cookies)))
    if not _csrf_ok(deps, cookies, form):
        return Response(403, render.message_page("Blocked", "Invalid CSRF token; reload and retry."))

    owner, name = _parse_repo(form.get("repo") or "", default_owner=client.login)
    if not name:
        return _handle_connect_form(
            deps=deps, cookies=cookies,
            error="Enter a repository as 'name' or 'owner/name'.")
    private = bool(form.get("private"))

    # A provider counts as selected when its checkbox is checked; a checked box
    # with a blank key is a user error we surface rather than silently drop.
    api_keys: dict = {}
    missing: list = []
    for pid in PROVIDER_ORDER:
        if not form.get(f"provider_{pid}"):
            continue
        key = (form.get(f"key_{pid}") or "").strip()
        if key:
            api_keys[pid] = key
        else:
            missing.append(pid)
    if missing:
        return _handle_connect_form(
            deps=deps, cookies=cookies,
            error=f"Checked {', '.join(missing)} but pasted no key for it.")

    # The operator is always the signed-in user (the engine gates their commands
    # on their login), even when the repo lives under an org they admin.
    try:
        result = provision(client, owner=owner, repo=name,
                           operator_login=client.login, api_keys=api_keys, private=private)
    except ValueError as exc:
        # Bad input (e.g. no provider selected) — re-show the form with the reason.
        return _handle_connect_form(deps=deps, cookies=cookies, error=str(exc))
    except GitHubError as exc:
        # Provisioning failed at GitHub, or locally (e.g. PyNaCl missing). Surface
        # an actionable message; the repo may be half-wired (retry is safe).
        return Response(502, render.message_page(
            "Could not finish connecting", _provision_error_html(exc),
            signed_in=_signed_in(deps, cookies)))
    except Exception as exc:  # noqa: BLE001 - unexpected; sanitized, never 500 silently
        return Response(502, render.message_page(
            "Could not connect",
            f"Provisioning failed unexpectedly ({escape(type(exc).__name__)}). The "
            "repo may be partially set up — re-submitting Connect is safe; it skips "
            "what's already done.", signed_in=_signed_in(deps, cookies)))

    return Response(200, render.connect_success(
        owner=owner, repo=name, panel=result.reviewers,
        signed_in=_signed_in(deps, cookies)))


def _parse_repo(raw: str, *, default_owner: str) -> "tuple[str, str]":
    """``(owner, name)`` from ``name`` (owner defaults to the signed-in user) or
    ``owner/name`` (an org the user admins). ``('', '')`` if malformed — empty, or
    more than one path segment."""
    raw = (raw or "").strip().strip("/")
    if not raw:
        return ("", "")
    if "/" in raw:
        owner, _, name = raw.partition("/")
        owner, name = owner.strip(), name.strip()
        if not owner or not name or "/" in name:
            return ("", "")
        return (owner, name)
    return (default_owner, raw)


def _provision_error_html(exc: GitHubError) -> str:
    """User-facing message for a provisioning failure. A status-less GitHubError is
    a LOCAL/config failure (e.g. PyNaCl missing) whose message is our own safe,
    actionable string — surface it. A status-bearing one is a GitHub API failure —
    show a curated line by code, never the raw response body (which could echo
    request data). Always note the repo may be partial and retry is safe."""
    if exc.status is None:
        head = escape(str(exc))
    else:
        head = {
            401: "GitHub rejected your sign-in — sign in again.",
            403: "GitHub denied the request — your account may lack admin access to "
                 "create this repo or set its secrets.",
            404: "GitHub couldn't find that — check the owner / repository name.",
            422: "GitHub rejected the request — the repository name may be invalid "
                 "or already taken.",
        }.get(exc.status, f"GitHub returned HTTP {exc.status}.")
    return (f"{head}<br><span class='meta'>The repo may be partially set up — "
            "re-submitting Connect is safe; it skips what's already done.</span>")


__all__ = ["Response", "Deps", "route"]
