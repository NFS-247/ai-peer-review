"""Server wiring: the operator-client factory must never leak the dev token in
a prod (OAuth-enabled) deployment."""

from front_door.app import config as config_mod
from front_door.app.gh import GitHub
from front_door.app.server import _operator_client_factory
from front_door.app.sessions import SID_COOKIE, SessionStore


def _cfg(**kw):
    base = dict(read_token="t", repos=("o/r",), dev_operator_token="ghp_dev")
    base.update(kw)
    return config_mod.Config(**base)


def test_dev_token_used_when_oauth_disabled():
    # Local dev: no OAuth -> the dev token is the operator identity.
    factory = _operator_client_factory(_cfg(), None)
    client = factory({})
    assert isinstance(client, GitHub) and client._token == "ghp_dev"


def test_dev_token_refused_when_oauth_enabled():
    # Prod with a dev token accidentally left set: an unauthenticated request
    # (no session) must NOT fall back to the dev identity.
    cfg = _cfg(oauth_client_id="cid", oauth_client_secret="sec")
    factory = _operator_client_factory(cfg, SessionStore())
    assert factory({}) is None


def test_session_token_used_when_present():
    cfg = _cfg(oauth_client_id="cid", oauth_client_secret="sec")
    s = SessionStore()
    sid, _csrf = s.create("ghp_user")
    factory = _operator_client_factory(cfg, s)
    client = factory({SID_COOKIE: sid})
    assert isinstance(client, GitHub) and client._token == "ghp_user"
