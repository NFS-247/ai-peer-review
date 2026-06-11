"""Request routing: ties gh + viewmodels + render together.

``route()`` is pure-ish — all I/O goes through an injected ``Deps`` (a read
client, an operator-client factory, config, and the current time), so the board,
inbox, and the action POST are all unit-testable with fakes and no network.
``server.py`` is the only part that touches sockets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from . import render
from .commands import VALID_ACTIONS, command_body
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


def route(method: str, path: str, *, cookies: dict, form: dict, deps: Deps) -> Response:
    if method == "GET" and path == "/healthz":
        return Response(200, "ok", "text/plain; charset=utf-8")

    if method == "GET" and path == "/":
        return Response(200, render.board_page(_gather(deps)))

    if method == "GET" and path == "/inbox":
        items = inbox_from_rows(_all_rows(_gather(deps)))
        return Response(200, render.inbox_page(items))

    if method == "GET" and path == "/login":
        return Response(200, render.message_page(
            "Sign in",
            "Writes (Approve/Block/Investigate) post a GitHub comment <b>as you</b>, "
            "so the engine accepts them as the operator. In production this is GitHub "
            "OAuth; for local dev set <code>FRONT_DOOR_DEV_TOKEN</code> to your token.",
        ))

    if method == "POST" and path == "/action":
        return _handle_action(cookies=cookies, form=form, deps=deps)

    return Response(404, render.message_page("Not found", "No such page."))


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

    client = deps.operator_client(cookies)
    if client is None:
        return Response(401, render.message_page(
            "Sign in required",
            "No operator identity. <a href='/login'>How to sign in.</a>"))

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
