"""Stdlib http.server adapter — the only socket-facing module.

Translates HTTP <-> the pure ``route()``. Swap this for FastAPI/Next.js later;
the tested core (viewmodels/commands/router/sessions/oauth) is unchanged.

The session store and OAuth client are created ONCE per process and shared
across requests (sessions must persist between the login redirect and later
actions); ``make_deps`` builds a per-request ``Deps`` around them.
"""

from __future__ import annotations

import time
import urllib.parse
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .gh import GitHub
from .oauth import OAuth
from .router import Deps, route
from .sessions import SID_COOKIE, SessionStore


def _operator_client_factory(cfg, sessions):
    """Return ``(cookies) -> operator GitHub client | None``.

    Prefer the OAuth session token (the user's own identity); fall back to the
    dev operator token for local dev. None when neither exists -> the router
    asks the user to sign in.
    """
    def factory(cookies: dict):
        if sessions is not None:
            token = sessions.token_for(cookies.get(SID_COOKIE))
            if token:
                return GitHub(token, api_base=cfg.api_base)
        if cfg.dev_operator_token:
            return GitHub(cfg.dev_operator_token, api_base=cfg.api_base)
        return None
    return factory


def make_deps(cfg, *, now_ts=None, sessions=None, oauth=None) -> Deps:
    return Deps(
        read=GitHub(cfg.read_token, api_base=cfg.api_base),
        operator_client=_operator_client_factory(cfg, sessions),
        cfg=cfg,
        now_ts=now_ts if now_ts is not None else time.time(),
        sessions=sessions,
        oauth=oauth,
    )


def _parse_cookies(header: str) -> dict:
    jar = http_cookies.SimpleCookie()
    try:
        jar.load(header or "")
    except http_cookies.CookieError:
        return {}
    return {k: m.value for k, m in jar.items()}


class _Handler(BaseHTTPRequestHandler):
    cfg = None        # set by serve()
    sessions = None   # shared SessionStore
    oauth = None      # shared OAuth

    def _dispatch(self, method: str):
        parsed = urllib.parse.urlsplit(self.path)
        query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        cookies = _parse_cookies(self.headers.get("Cookie", ""))
        form: dict = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            form = {k: v[-1] for k, v in urllib.parse.parse_qs(raw).items()}
        deps = make_deps(self.cfg, sessions=self.sessions, oauth=self.oauth)
        try:
            resp = route(method, parsed.path, cookies=cookies, form=form, deps=deps, query=query)
        except Exception as exc:  # noqa: BLE001 - never leak a stack trace
            self.send_error(500, f"internal error: {type(exc).__name__}")
            return
        self.send_response(resp.status)
        self.send_header("Content-Type", resp.content_type)
        for k, v in resp.headers.items():
            self.send_header(k, v)
        data = resp.body.encode("utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    def do_GET(self):  # noqa: N802
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802
        self._dispatch("POST")

    def log_message(self, *args):  # quieter default logging
        return


def serve(cfg) -> None:
    _Handler.cfg = cfg
    _Handler.sessions = SessionStore()
    _Handler.oauth = OAuth(cfg.oauth_client_id, cfg.oauth_client_secret, scope=cfg.oauth_scope)
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), _Handler)
    mode = "OAuth" if cfg.oauth_enabled() else ("dev-token" if cfg.dev_operator_token else "read-only")
    print(f"Front Door on http://{cfg.host}:{cfg.port}  "
          f"(repos: {', '.join(cfg.repos) or 'none'}; auth: {mode})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


__all__ = ["serve", "make_deps"]
