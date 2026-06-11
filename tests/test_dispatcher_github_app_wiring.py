"""Engine wiring for GitHub App auth: GitHubAPI.from_app, _build_github_api, config."""

from types import SimpleNamespace

import pytest

from scripts.dispatcher import main
from scripts.dispatcher.config import load_from_env
from scripts.dispatcher.github_api import GitHubAPI
from scripts.dispatcher.github_app import GitHubAppError


class _FakeApp:
    def __init__(self):
        self.calls = []

    def token_for_repo(self, owner, repo):
        self.calls.append((owner, repo))
        return "ghs_installation_token"


def test_from_app_uses_installation_token():
    app = _FakeApp()
    api = GitHubAPI.from_app(
        app_id=1, private_key_pem="unused", owner="o", repo="r", app=app
    )
    assert isinstance(api, GitHubAPI)
    assert api._token == "ghs_installation_token"
    assert (api._owner, api._repo) == ("o", "r")
    assert app.calls == [("o", "r")]


def test_build_github_api_prefers_app_when_configured(monkeypatch):
    captured = {}

    def fake_from_app(*, app_id, private_key_pem, owner, repo):
        captured.update(app_id=app_id, key=private_key_pem, owner=owner, repo=repo)
        return "APP_CLIENT"

    monkeypatch.setattr(main.GitHubAPI, "from_app", fake_from_app)
    cfg = SimpleNamespace(
        github_app_id="123", github_app_private_key="PEM",
        github_token="tok", repo_owner="o", repo_name="r",
    )
    assert main._build_github_api(cfg) == "APP_CLIENT"
    assert captured == {"app_id": "123", "key": "PEM", "owner": "o", "repo": "r"}


def test_build_github_api_falls_back_to_token():
    cfg = SimpleNamespace(
        github_app_id="", github_app_private_key="",
        github_token="tok", repo_owner="o", repo_name="r",
    )
    api = main._build_github_api(cfg)
    assert isinstance(api, GitHubAPI)
    assert api._token == "tok"


def test_build_github_api_app_requires_both_halves():
    # Only an id, or only a key, is not enough — fall back to the token.
    for app_id, key in (("123", ""), ("", "PEM")):
        cfg = SimpleNamespace(
            github_app_id=app_id, github_app_private_key=key,
            github_token="tok", repo_owner="o", repo_name="r",
        )
        assert main._build_github_api(cfg)._token == "tok"


def test_config_loads_app_credentials():
    env = {
        "GITHUB_TOKEN": "tok",
        "GITHUB_APP_ID": " 12345 ",
        "GITHUB_APP_PRIVATE_KEY":
            "-----BEGIN RSA PRIVATE KEY-----\nABC\n-----END RSA PRIVATE KEY-----\n",
    }
    cfg = load_from_env(env)
    assert cfg.github_app_id == "12345"                                   # stripped
    assert cfg.github_app_private_key.startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert cfg.github_app_private_key.endswith("-----END RSA PRIVATE KEY-----")  # \n trimmed


def test_config_app_credentials_default_empty():
    cfg = load_from_env({"GITHUB_TOKEN": "tok"})
    assert cfg.github_app_id == ""
    assert cfg.github_app_private_key == ""


def _raises(exc):
    def _from_app(**kw):
        raise exc
    return _from_app


@pytest.mark.parametrize("exc", [
    GitHubAppError("github 503", status=503),    # 5xx -> transient
    GitHubAppError("rate limited", status=429),   # 429 -> transient
    GitHubAppError("dns failure", status=None),   # network -> transient
])
def test_build_github_api_falls_back_on_transient(monkeypatch, capsys, exc):
    # A transient fault degrades to GITHUB_TOKEN (loudly) rather than losing the run.
    monkeypatch.setattr(main.GitHubAPI, "from_app", _raises(exc))
    cfg = SimpleNamespace(
        github_app_id="1", github_app_private_key="K",
        github_token="tok", repo_owner="o", repo_name="r",
    )
    api = main._build_github_api(cfg)
    assert api._token == "tok"
    assert "fall" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("exc", [
    GitHubAppError("not installed", status=404),  # 4xx -> misconfig
    GitHubAppError("bad creds", status=401),       # 4xx -> misconfig
    ValueError("malformed private key"),            # construction -> misconfig
])
def test_build_github_api_raises_on_misconfig(monkeypatch, exc):
    # A misconfig must NOT silently fall back to GITHUB_TOKEN — that would mask it
    # behind the shared bucket. It propagates (run() turns it into a clean message).
    monkeypatch.setattr(main.GitHubAPI, "from_app", _raises(exc))
    cfg = SimpleNamespace(
        github_app_id="1", github_app_private_key="K",
        github_token="tok", repo_owner="o", repo_name="r",
    )
    with pytest.raises(type(exc)):
        main._build_github_api(cfg)


def test_build_github_api_transient_without_token_raises(monkeypatch):
    # Transient, but no GITHUB_TOKEN to fall back to -> propagate.
    monkeypatch.setattr(
        main.GitHubAPI, "from_app", _raises(GitHubAppError("x", status=503))
    )
    cfg = SimpleNamespace(
        github_app_id="1", github_app_private_key="K",
        github_token="", repo_owner="o", repo_name="r",
    )
    with pytest.raises(GitHubAppError):
        main._build_github_api(cfg)
