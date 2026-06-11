"""Server-rendered HTML (stdlib only, no template engine).

Deliberately boring — "the value is the loop behind it, not the chrome"
(UI-PLAN.md). All dynamic values are HTML-escaped. Swap this module for a real
frontend without touching the tested core.
"""

from __future__ import annotations

from html import escape
from typing import Iterable

from .viewmodels import (
    STATUS_ESCALATED,
    STATUS_PAUSED,
    STATUS_READY,
    STATUS_REVIEWING,
    STATUS_SECRET_MISSING,
    BoardRow,
    InboxItem,
    ProjectSpend,
    RepoView,
)


_STATUS_COLOR = {
    STATUS_READY: "#1a7f37",
    STATUS_ESCALATED: "#9a6700",
    STATUS_PAUSED: "#6e7781",
    STATUS_REVIEWING: "#0969da",
    STATUS_SECRET_MISSING: "#cf222e",
}

_CSS = """
body{font:14px -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#f6f8fa;color:#1f2328}
header{background:#24292f;color:#fff;padding:12px 20px;display:flex;gap:20px;align-items:center}
header a{color:#fff;text-decoration:none;opacity:.85}header a:hover{opacity:1}
main{max-width:1000px;margin:20px auto;padding:0 16px}
.card{background:#fff;border:1px solid #d0d7de;border-radius:8px;margin:14px 0;overflow:hidden}
.card h2{font-size:15px;margin:0;padding:10px 14px;background:#f6f8fa;border-bottom:1px solid #d0d7de;
  display:flex;justify-content:space-between}
.row{padding:10px 14px;border-bottom:1px solid #eaeef2;display:flex;gap:12px;align-items:center}
.row:last-child{border-bottom:0}
.badge{font-size:11px;font-weight:600;color:#fff;border-radius:10px;padding:2px 8px;white-space:nowrap}
.meta{color:#6e7781;font-size:12px}
.grow{flex:1;min-width:0}.title{font-weight:600}.title a{color:#0969da;text-decoration:none}
.rv{font-size:12px;margin-right:8px}.ok{color:#1a7f37}.no{color:#cf222e}
form.inline{display:inline}button,input[type=text]{font:13px inherit}
button{background:#1f883d;color:#fff;border:0;border-radius:6px;padding:5px 10px;cursor:pointer}
button.warn{background:#9a6700}button.danger{background:#cf222e}
input[type=text]{padding:5px 8px;border:1px solid #d0d7de;border-radius:6px}
.empty{padding:24px;text-align:center;color:#6e7781}
.note{background:#fff8c5;border:1px solid #d4a72c55;padding:8px 14px;border-radius:6px;margin:12px 0}
"""


def _layout(title: str, body: str, *, signed_in: bool = False, refresh: int = 0) -> str:
    meta_refresh = f"<meta http-equiv='refresh' content='{int(refresh)}'>" if refresh else ""
    auth = (
        "<a href='/logout'>Sign out</a>" if signed_in
        else "<a href='/login'>Sign in</a>"
    )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"{meta_refresh}<title>{escape(title)}</title><style>{_CSS}</style></head><body>"
        "<header><strong>Front Door</strong>"
        "<a href='/'>Projects</a><a href='/inbox'>Approvals</a>"
        "<span style='flex:1'></span>"
        f"{auth}</header>"
        f"<main>{body}</main></body></html>"
    )


def _badge(status: str) -> str:
    color = _STATUS_COLOR.get(status, "#6e7781")
    return f"<span class='badge' style='background:{color}'>{escape(status)}</span>"


def _reviewers(reviewers: dict) -> str:
    if not reviewers:
        return "<span class='meta'>no verdicts yet</span>"
    out = []
    for name, verdict in sorted(reviewers.items()):
        cls = "ok" if verdict == "approve" else ("no" if verdict == "request_changes" else "meta")
        out.append(f"<span class='rv {cls}'>{escape(name)}: {escape(verdict)}</span>")
    return "".join(out)


def _spend_line(spend: ProjectSpend) -> str:
    if not spend or spend.total_24h_usd <= 0:
        return "<span class='meta'>24h spend: $0.00</span>"
    parts = " · ".join(
        f"{escape(p)} ${a:.2f}" for p, a in sorted(spend.by_provider.items(), key=lambda kv: -kv[1])
    )
    extra = f" ({parts})" if parts else ""
    return f"<span class='meta'>24h spend: ${spend.total_24h_usd:.2f}{extra}</span>"


def board_page(views: "list[RepoView]", *, signed_in: bool = False) -> str:
    if not views:
        body = "<div class='empty'>No projects configured. Set <code>FRONT_DOOR_REPOS</code>.</div>"
        return _layout("Projects", body, signed_in=signed_in)
    cards = []
    for v in views:
        if v.error:
            inner = f"<div class='empty' style='color:#cf222e'>Couldn't read this repo — {escape(v.error)}</div>"
            cards.append(f"<div class='card'><h2><span>{escape(v.repo)}</span></h2>{inner}</div>")
            continue
        row_html = "".join(_board_row(r) for r in v.rows) if v.rows \
            else "<div class='empty'>No open PRs.</div>"
        cards.append(
            f"<div class='card'><h2><span>{escape(v.repo)} {_health(v.rows)}</span>"
            f"{_spend_line(v.spend)}</h2>{row_html}</div>"
        )
    return _layout("Projects", "".join(cards), signed_in=signed_in, refresh=30)


def _health(rows: "list[BoardRow]") -> str:
    """Compact per-repo status rollup, e.g. '2 reviewing · 1 escalated'."""
    if not rows:
        return ""
    counts: dict = {}
    for r in rows:
        counts[r.status] = counts.get(r.status, 0) + 1
    order = [STATUS_ESCALATED, STATUS_READY, STATUS_REVIEWING, STATUS_PAUSED, STATUS_SECRET_MISSING]
    parts = [f"{counts[s]} {s}" for s in order if s in counts]
    return f"<span class='meta' style='font-weight:400'>({' · '.join(parts)})</span>" if parts else ""


def _board_row(r: BoardRow) -> str:
    tier = f" · {escape(r.tier)}" if r.tier else ""
    rnd = f" · round {r.round}" if r.round else ""
    return (
        "<div class='row'>"
        f"{_badge(r.status)}"
        f"<div class='grow'><div class='title'><a href='{escape(r.url)}'>"
        f"#{r.number} {escape(r.title)}</a></div>"
        f"<div>{_reviewers(r.reviewers)}</div>"
        f"<span class='meta'>${r.cost_usd:.2f}{tier}{rnd}</span></div>"
        "</div>"
    )


def inbox_page(items: "Iterable[InboxItem]", *, signed_in: bool = False, csrf: str = "") -> str:
    items = list(items)
    if not items:
        return _layout("Approvals", "<div class='card'><div class='empty'>"
                       "Nothing waiting on you. 🎉</div></div>", signed_in=signed_in, refresh=30)
    rows = "".join(_inbox_row(it, csrf) for it in items)
    note = ("<div class='note'>Actions post an <b>OPERATOR</b> command as you. "
            "Approve marks the PR ready — you still click merge on GitHub.</div>")
    return _layout("Approvals", note + f"<div class='card'>{rows}</div>",
                   signed_in=signed_in, refresh=30)


def _inbox_row(it: InboxItem, csrf: str = "") -> str:
    csrf_field = f"<input type='hidden' name='csrf' value='{escape(csrf)}'>" if csrf else ""

    def form(action: str, label: str, cls: str = "", text_field: bool = False) -> str:
        ti = ("<input type='text' name='text' placeholder='reason/note' "
              "style='width:160px'>") if text_field else ""
        c = f" class='{cls}'" if cls else ""
        return (
            "<form class='inline' method='post' action='/action'>"
            f"<input type='hidden' name='repo' value='{escape(it.repo)}'>"
            f"<input type='hidden' name='number' value='{it.number}'>"
            f"<input type='hidden' name='action' value='{action}'>"
            f"{csrf_field}{ti}<button{c}>{escape(label)}</button></form> "
        )
    actions = (
        form("approve", "✓ Approve")
        + form("block", "Block", "danger", text_field=True)
        + form("investigate", "Investigate", "warn", text_field=True)
    )
    return (
        "<div class='row'>"
        f"{_badge(it.status)}"
        f"<div class='grow'><div class='title'><a href='{escape(it.url)}'>"
        f"{escape(it.repo)} #{it.number} {escape(it.title)}</a></div>"
        f"<div>{_reviewers(it.reviewers)} <span class='meta'>· ${it.cost_usd:.2f}</span></div></div>"
        f"<div>{actions}</div>"
        "</div>"
    )


def message_page(title: str, html_body: str, *, signed_in: bool = False) -> str:
    return _layout(title, f"<div class='card'><div style='padding:14px'>{html_body}</div></div>",
                   signed_in=signed_in)


__all__ = ["board_page", "inbox_page", "message_page"]
