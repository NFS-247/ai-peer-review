"""Router tests for the OAuth login flow, sessions, and CSRF on actions."""

from front_door.app import config as config_mod
from front_door.app.router import Deps, route
from front_door.app.sessions import SID_COOKIE, STATE_COOKIE, SessionStore


def _state_cookie_value(resp) -> str:
    """Pull the fd_oauth_state value out of a /login response's Set-Cookie."""
    sc = resp.headers["Set-Cookie"]
    assert sc.startswith(f"{STATE_COOKIE}=")
    return sc.split(";")[0].split("=", 1)[1]

REPO = "NFS-247/StockTrader"


class MinRead:
    """Minimal board source (auth tests don't need a populated board)."""
    def list_open_pulls(self, repo): return []
    def list_labels(self, repo, n): return []
    def list_issue_comments(self, repo, n): return []
    def find_issue_body_by_marker(self, repo, marker): return ""


class FakeOAuth:
    def __init__(self, token="ghp_live"):
        self._token = token

    def configured(self): return True

    def authorize_url(self, *, state, redirect_uri):
        return f"https://github.com/login/oauth/authorize?state={state}&redirect_uri={redirect_uri}"

    def exchange_code(self, *, code, redirect_uri):
        return self._token


class FakeOperator:
    def __init__(self):
        self.posted = []

    def post_issue_comment(self, repo, number, body):
        self.posted.append((repo, number, body))
        return {"id": 1}


def _cfg():
    return config_mod.Config(
        read_token="t", repos=(REPO,),
        oauth_client_id="cid", oauth_client_secret="sec",
        public_base_url="https://app.example",
    )


def _deps(sessions, oauth, operator=None):
    op = operator or FakeOperator()

    def operator_client(cookies):
        if sessions.token_for(cookies.get(SID_COOKIE)):
            return op
        return None

    return Deps(read=MinRead(), operator_client=operator_client, cfg=_cfg(),
                now_ts=0.0, sessions=sessions, oauth=oauth), op


# ---- login redirect ---------------------------------------------------------
def test_login_redirects_to_github_with_state():
    s = SessionStore()
    deps, _ = _deps(s, FakeOAuth())
    r = route("GET", "/login", cookies={}, form={}, deps=deps)
    assert r.status == 303
    assert r.headers["Location"].startswith("https://github.com/login/oauth/authorize")
    assert "state=" in r.headers["Location"]
    # redirect_uri is built from public_base_url
    assert "app.example/auth/callback" in r.headers["Location"]
    # state is also pinned to the browser via an httponly, Secure (https) cookie
    sc = r.headers["Set-Cookie"]
    assert sc.startswith(f"{STATE_COOKIE}=") and "HttpOnly" in sc and "Secure" in sc


def test_login_dev_message_when_oauth_unconfigured():
    deps = Deps(read=MinRead(), operator_client=lambda c: None,
                cfg=config_mod.Config(read_token="t", repos=(REPO,)),
                now_ts=0.0, sessions=SessionStore(), oauth=None)
    r = route("GET", "/login", cookies={}, form={}, deps=deps)
    assert r.status == 200 and "OAuth isn't configured" in r.body


# ---- callback ---------------------------------------------------------------
def test_callback_exchanges_code_and_sets_session_cookie():
    s = SessionStore()
    deps, _ = _deps(s, FakeOAuth(token="ghp_live"))
    # Full flow: /login mints the state + sets the binding cookie; the browser
    # echoes both back on the callback.
    login = route("GET", "/login", cookies={}, form={}, deps=deps)
    state = login.headers["Location"].split("state=")[1].split("&")[0]
    cookie_val = _state_cookie_value(login)
    r = route("GET", "/auth/callback", cookies={STATE_COOKIE: cookie_val}, form={},
              deps=deps, query={"code": "abc", "state": state})
    assert r.status == 303 and r.headers["Location"] == "/"
    set_cookie = r.headers["Set-Cookie"]
    assert set_cookie.startswith(f"{SID_COOKIE}=") and "HttpOnly" in set_cookie
    assert "Secure" in set_cookie  # public_base_url is https
    # the new session resolves to the exchanged token
    sid = set_cookie.split(";")[0].split("=", 1)[1]
    assert s.token_for(sid) == "ghp_live"


def test_callback_rejects_bad_state():
    s = SessionStore()
    deps, _ = _deps(s, FakeOAuth())
    r = route("GET", "/auth/callback", cookies={}, form={},
              deps=deps, query={"code": "abc", "state": "forged"})
    assert r.status == 400 and "Invalid or expired state" in r.body


def test_callback_rejects_state_not_bound_to_this_browser():
    """Login-CSRF guard: a server-valid state with NO matching browser cookie is
    rejected, so an attacker can't complete the callback in a victim's browser."""
    s = SessionStore()
    deps, op = _deps(s, FakeOAuth())
    state = s.new_state()  # a live, server-valid state (e.g. minted by the attacker)
    # Victim's browser has no fd_oauth_state cookie (or a different one).
    r = route("GET", "/auth/callback", cookies={}, form={},
              deps=deps, query={"code": "abc", "state": state})
    assert r.status == 400 and "Invalid or expired state" in r.body
    r2 = route("GET", "/auth/callback", cookies={STATE_COOKIE: "different"}, form={},
               deps=deps, query={"code": "abc", "state": state})
    assert r2.status == 400


# ---- CSRF on actions --------------------------------------------------------
def test_action_requires_csrf_when_session_present():
    s = SessionStore()
    deps, op = _deps(s, FakeOAuth())
    sid, csrf = s.create("ghp_live")
    cookies = {SID_COOKIE: sid}

    # missing csrf -> blocked
    r = route("POST", "/action", cookies=cookies,
              form={"repo": REPO, "number": "114", "action": "approve"}, deps=deps)
    assert r.status == 403 and op.posted == []

    # wrong csrf -> blocked
    r = route("POST", "/action", cookies=cookies,
              form={"repo": REPO, "number": "114", "action": "approve", "csrf": "nope"}, deps=deps)
    assert r.status == 403 and op.posted == []

    # correct csrf -> posts as the operator
    r = route("POST", "/action", cookies=cookies,
              form={"repo": REPO, "number": "114", "action": "approve", "csrf": csrf}, deps=deps)
    assert r.status == 303
    assert op.posted == [(REPO, 114, "OPERATOR APPROVE")]


def test_action_without_session_is_rejected_in_oauth_mode():
    """The critical bypass: in OAuth mode a request with no session must NOT act.
    _csrf_ok fails closed when the session layer exists but the caller has none,
    so an unauthenticated cross-site POST never reaches the operator client."""
    s = SessionStore()
    deps, op = _deps(s, FakeOAuth())  # session layer present, but request has no sid
    r = route("POST", "/action", cookies={},
              form={"repo": REPO, "number": "114", "action": "approve", "csrf": "anything"},
              deps=deps)
    assert r.status == 403 and op.posted == []


def test_logout_clears_cookie_and_session():
    s = SessionStore()
    deps, _ = _deps(s, FakeOAuth())
    sid, _csrf = s.create("ghp_live")
    r = route("GET", "/logout", cookies={SID_COOKIE: sid}, form={}, deps=deps)
    assert r.status == 303 and r.headers["Location"] == "/"
    assert "Max-Age=0" in r.headers["Set-Cookie"]
    assert s.token_for(sid) is None  # session destroyed
