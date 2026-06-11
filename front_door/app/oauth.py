"""GitHub OAuth (web application flow), stdlib only.

Builds the authorize redirect and exchanges the callback code for the user's
access token. That token is what the front door uses to post operator commands
**as that user**, which is what the engine requires (it verifies the commenter's
GitHub id). The token is then held server-side (see sessions.py), never in the
browser.

Needs a registered GitHub OAuth App: GITHUB_OAUTH_CLIENT_ID / _CLIENT_SECRET, and
a public callback URL (FRONT_DOOR_PUBLIC_URL + /auth/callback). ``repo`` scope is
the default so the user can comment on / read their private repos.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Optional

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"


class OAuthError(RuntimeError):
    pass


class OAuth:
    def __init__(self, client_id: str, client_secret: str, *, scope: str = "repo",
                 timeout: int = 20) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope
        self._timeout = timeout

    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def authorize_url(self, *, state: str, redirect_uri: str) -> str:
        q = urllib.parse.urlencode({
            "client_id": self.client_id,
            "scope": self.scope,
            "state": state,
            "redirect_uri": redirect_uri,
            "allow_signup": "false",
        })
        return f"{AUTHORIZE_URL}?{q}"

    def exchange_code(self, *, code: str, redirect_uri: str) -> str:
        """Exchange an authorization code for an access token (raises on failure)."""
        data = urllib.parse.urlencode({
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
        }).encode("utf-8")
        req = urllib.request.Request(
            TOKEN_URL, data=data, method="POST",
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        token = payload.get("access_token")
        if not token:
            raise OAuthError(f"no access_token in response ({payload.get('error', 'unknown')})")
        return token


__all__ = ["OAuth", "OAuthError", "AUTHORIZE_URL", "TOKEN_URL"]
