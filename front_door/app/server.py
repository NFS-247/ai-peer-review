"""Stdlib http.server adapter — the only socket-facing module.

Translates HTTP <-> the pure ``route()``. Swap this for FastAPI/Next.js later;
the tested core (viewmodels/commands/router) is unchanged.
"""

from __future__ import annotations

import time
import urllib.parse
from http import cookies as http_cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as config_mod
from .gh import GitHub
from .router import Deps, route


def _operator_client_factory(cfg):
    """Return ``(cookies) -> operator GitHub client | None``.

    Dev: a configured FRONT_DOOR_DEV_TOKEN is the operator identity. Prod: map a
    session cookie set by the GitHub OAuth callback to that user's token — wire
    it here (look up cookies['session'] -> stored token). Returns None when there
    is no operator identity, so the router asks the user to sign in.
    """
    def factory(cookies: dict):
        # TODO(prod): cookies['session'] -> OAuth-stored token for this user.
        if cfg.dev_operator_token:
            return GitHub(cfg.dev_operator_token, api_base=cfg.api_base)
        return None
    return factory


def make_deps(cfg, *, now_ts=None) -> Deps:
    return Deps(
        read=GitHub(cfg.read_token, api_base=cfg.api_base),
        operator_client=_operator_client_factory(cfg),
        cfg=cfg,
        now_ts=now_ts if now_ts is not None else time.time(),
    )


def _parse_cookies(header: str) -> dict:
    jar = http_cookies.SimpleCookie()
    try:
        jar.load(header or "")
    except http_cookies.CookieError:
        return {}
    return {k: m.value for k, m in jar.items()}


class _Handler(BaseHTTPRequestHandler):
    cfg = None  # set by serve()

    def _dispatch(self, method: str):
        parsed = urllib.parse.urlsplit(self.path)
        cookies = _parse_cookies(self.headers.get("Cookie", ""))
        form: dict = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            form = {k: v[-1] for k, v in urllib.parse.parse_qs(raw).items()}
        deps = make_deps(self.cfg)
        try:
            resp = route(method, parsed.path, cookies=cookies, form=form, deps=deps)
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
    httpd = ThreadingHTTPServer((cfg.host, cfg.port), _Handler)
    print(f"Front Door on http://{cfg.host}:{cfg.port}  (repos: {', '.join(cfg.repos) or 'none'})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


__all__ = ["serve", "make_deps"]
