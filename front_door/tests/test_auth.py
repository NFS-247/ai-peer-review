"""Tests for the session store and the OAuth flow."""

import json
from unittest import mock

from front_door.app import oauth as oauth_mod
from front_door.app.sessions import SessionStore


# ---- sessions ---------------------------------------------------------------
def test_session_roundtrip_and_token_is_server_side():
    s = SessionStore()
    sid, csrf = s.create("ghp_secret")
    assert s.token_for(sid) == "ghp_secret"   # looked up by opaque id
    assert s.csrf_for(sid) == csrf
    assert sid != "ghp_secret" and csrf != "ghp_secret"  # neither leaks the token


def test_session_unknown_and_deleted():
    s = SessionStore()
    assert s.token_for(None) is None
    assert s.token_for("nope") is None
    sid, _ = s.create("t")
    s.delete(sid)
    assert s.token_for(sid) is None


def test_session_expires():
    clock = {"t": 1000.0}
    s = SessionStore(ttl_seconds=100, now=lambda: clock["t"])
    sid, _ = s.create("t")
    clock["t"] = 1101.0  # past ttl
    assert s.token_for(sid) is None


def test_oauth_state_is_one_shot_and_expires():
    clock = {"t": 1000.0}
    s = SessionStore(state_ttl_seconds=60, now=lambda: clock["t"])
    st = s.new_state()
    assert s.consume_state(st) is True
    assert s.consume_state(st) is False      # cannot replay
    st2 = s.new_state()
    clock["t"] = 1100.0
    assert s.consume_state(st2) is False     # expired
    assert s.consume_state(None) is False


# ---- oauth ------------------------------------------------------------------
def test_authorize_url_carries_state_and_scope():
    o = oauth_mod.OAuth("cid", "secret", scope="repo")
    url = o.authorize_url(state="xyz", redirect_uri="https://app.x/auth/callback")
    assert url.startswith(oauth_mod.AUTHORIZE_URL + "?")
    assert "client_id=cid" in url and "state=xyz" in url and "scope=repo" in url
    assert "redirect_uri=https%3A%2F%2Fapp.x%2Fauth%2Fcallback" in url


def test_exchange_code_returns_token():
    o = oauth_mod.OAuth("cid", "secret")

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"access_token": "ghp_live"}).encode()

    with mock.patch.object(oauth_mod.urllib.request, "urlopen", lambda req, timeout=0: _Resp()):
        tok = o.exchange_code(code="abc", redirect_uri="https://app.x/auth/callback")
    assert tok == "ghp_live"


def test_exchange_code_raises_without_token():
    o = oauth_mod.OAuth("cid", "secret")

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"error": "bad_verification_code"}).encode()

    with mock.patch.object(oauth_mod.urllib.request, "urlopen", lambda req, timeout=0: _Resp()):
        try:
            o.exchange_code(code="x", redirect_uri="u")
            assert False, "expected OAuthError"
        except oauth_mod.OAuthError as exc:
            assert "bad_verification_code" in str(exc)


def test_configured():
    assert oauth_mod.OAuth("a", "b").configured() is True
    assert oauth_mod.OAuth("", "").configured() is False
