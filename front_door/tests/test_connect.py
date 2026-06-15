"""Connect flow (self-serve provisioning UI) — router-level tests.

Exercises GET/POST /connect through the REAL provision() orchestration against a
fake provisioning client, so the route, provider-checkbox parsing, roster/secret
wiring, CSRF, and the signed-in gate are all covered without GitHub.
"""

import json
import types

import pytest

from front_door.app import provision as P
from front_door.app.router import Deps, route
from front_door.app.sessions import SID_COOKIE


class FakeProvisioner:
    """Satisfies provision.ProvisioningClient and carries a .login like the real
    GitHubProvisioner."""

    def __init__(self, login="alice", existing=()):
        self.login = login
        self._existing = set(existing)
        self.created: list = []
        self.files: dict = {}
        self.secrets: dict = {}

    def repo_exists(self, owner, repo):
        return (owner, repo) in self._existing

    def create_repo(self, owner, repo, *, private=True):
        self.created.append((owner, repo, private))
        self._existing.add((owner, repo))

    def put_file(self, owner, repo, path, content, message):
        self.files[(owner, repo, path)] = content

    def set_secret(self, owner, repo, name, value):
        self.secrets[(owner, repo, name)] = value


class FakeSessions:
    """Minimal session layer that, like the real SessionStore, only resolves a
    csrf/token for the EXACT sid it issued — so 'session present but wrong/no SID
    cookie' fails closed, matching production."""

    def __init__(self, csrf="good", token="tok", sid="sid1"):
        self._csrf = csrf
        self._token = token
        self._sid = sid

    def csrf_for(self, sid):
        return self._csrf if sid == self._sid else None

    def token_for(self, sid):
        return self._token if sid == self._sid else None


def _deps(provisioner=None, *, sessions=None):
    return Deps(
        read=None,
        operator_client=lambda c: None,
        cfg=types.SimpleNamespace(repos=()),
        now_ts=0.0,
        sessions=sessions,
        oauth=None,
        provisioning_client=lambda cookies: provisioner,
    )


def _get(deps, cookies=None):
    return route("GET", "/connect", cookies=cookies or {}, form={}, deps=deps)


def _post(deps, form, cookies=None):
    return route("POST", "/connect", cookies=cookies or {}, form=form, deps=deps)


# ---- GET --------------------------------------------------------------------
def test_get_connect_prompts_signin_when_no_identity():
    resp = _get(_deps(provisioner=None))
    assert resp.status == 200
    assert "Sign in" in resp.body


def test_get_connect_renders_form_with_all_provider_checkboxes():
    resp = _get(_deps(FakeProvisioner(login="octocat")))
    assert resp.status == 200
    assert "Connect a repository" in resp.body
    assert "octocat" in resp.body
    for pid in ("anthropic", "openai", "gemini", "xai"):
        assert f"provider_{pid}" in resp.body and f"key_{pid}" in resp.body


# ---- POST: happy path -------------------------------------------------------
def test_post_connect_subset_wires_only_selected_providers():
    prov = FakeProvisioner(login="alice")
    form = {"repo": "idea", "private": "1",
            "provider_anthropic": "1", "key_anthropic": "sk-ant",
            "provider_gemini": "1", "key_gemini": "g-key"}
    resp = _post(_deps(prov), form)
    assert resp.status == 200
    # selected -> these secrets set; unselected -> absent
    assert prov.secrets[("alice", "idea", "ANTHROPIC_API_KEY")] == "sk-ant"
    assert prov.secrets[("alice", "idea", "GEMINI_API_KEY")] == "g-key"
    assert ("alice", "idea", "OPENAI_API_KEY") not in prov.secrets
    assert ("alice", "idea", "XAI_API_KEY") not in prov.secrets
    assert ("alice", "idea", "DISPATCHER_VERDICT_SECRET") in prov.secrets
    # config + workflow committed; roster matches the selection
    cfg = prov.files[("alice", "idea", P.CONFIG_PATH)]
    assert '"high_stakes_reviewers": [' in cfg
    assert "claude" in cfg and "gemini" in cfg and "gpt" not in cfg
    assert ("alice", "idea", P.WORKFLOW_PATH) in prov.files
    # success page names the chosen panel
    assert "claude" in resp.body and "gemini" in resp.body and "wired" in resp.body


def test_post_connect_creates_repo_as_signed_in_user():
    prov = FakeProvisioner(login="alice")  # repo does not exist -> created
    _post(_deps(prov), {"repo": "fresh", "provider_openai": "1", "key_openai": "k"})
    assert prov.created == [("alice", "fresh", False)]  # 'private' unchecked -> public


def test_post_connect_owner_slash_name_targets_an_org():
    # 'owner/name' provisions under an org the user admins; operator stays the user.
    prov = FakeProvisioner(login="alice")
    resp = _post(_deps(prov), {"repo": "acme/idea", "provider_openai": "1", "key_openai": "k"})
    assert resp.status == 200
    assert prov.created == [("acme", "idea", False)]
    cfg = json.loads(prov.files[("acme", "idea", P.CONFIG_PATH)])
    assert cfg["operator_github_login"] == "alice"
    assert "acme/idea" in resp.body


# ---- POST: validation -------------------------------------------------------
def test_post_connect_requires_a_repo_name():
    prov = FakeProvisioner()
    resp = _post(_deps(prov), {"provider_openai": "1", "key_openai": "k"})
    assert resp.status == 200
    assert "repository" in resp.body.lower()
    assert prov.files == {}  # nothing provisioned


def test_post_connect_checked_provider_without_key_is_an_error():
    prov = FakeProvisioner()
    resp = _post(_deps(prov), {"repo": "r", "provider_anthropic": "1", "key_anthropic": "  "})
    assert resp.status == 200
    assert "anthropic" in resp.body.lower() and "key" in resp.body.lower()
    assert prov.secrets == {}


def test_post_connect_no_provider_selected_reshows_form_with_reason():
    prov = FakeProvisioner()
    resp = _post(_deps(prov), {"repo": "r"})
    assert resp.status == 200
    assert "at least one provider" in resp.body.lower()
    assert prov.files == {}


# ---- POST: auth / CSRF ------------------------------------------------------
def test_post_connect_without_identity_is_401():
    resp = _post(_deps(provisioner=None), {"repo": "r", "provider_openai": "1", "key_openai": "k"})
    assert resp.status == 401


def test_post_connect_rejects_bad_csrf_when_session_present():
    prov = FakeProvisioner()
    deps = _deps(prov, sessions=FakeSessions(csrf="good", sid="sid1"))
    resp = _post(deps, {"repo": "r", "provider_openai": "1", "key_openai": "k", "csrf": "bad"},
                 cookies={SID_COOKIE: "sid1"})
    assert resp.status == 403
    assert prov.files == {}


def test_post_connect_accepts_matching_csrf_with_valid_session_cookie():
    prov = FakeProvisioner()
    deps = _deps(prov, sessions=FakeSessions(csrf="good", sid="sid1"))
    resp = _post(deps, {"repo": "r", "provider_openai": "1", "key_openai": "k", "csrf": "good"},
                 cookies={SID_COOKIE: "sid1"})
    assert resp.status == 200
    assert ("alice", "r", "OPENAI_API_KEY") in prov.secrets


def test_post_connect_fails_closed_when_session_present_but_no_sid_cookie():
    # Session layer configured but the request carries no valid SID -> csrf_for
    # returns None -> reject. 'No session' must never be a CSRF bypass.
    prov = FakeProvisioner()
    deps = _deps(prov, sessions=FakeSessions(csrf="good", sid="sid1"))
    resp = _post(deps, {"repo": "r", "provider_openai": "1", "key_openai": "k", "csrf": "good"})
    assert resp.status == 403
    assert prov.files == {}
