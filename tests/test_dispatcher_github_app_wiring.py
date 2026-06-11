"""Engine wiring for GitHub App auth: GitHubAPI.from_app, _build_github_api, config."""

from types import SimpleNamespace

import pytest

from scripts.dispatcher import main
from scripts.dispatcher.config import load_from_env
from scripts.dispatcher.github_api import GitHubAPI


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


def _boom(**kw):
    raise RuntimeError("mint blew up")


def test_build_github_api_falls_back_when_mint_fails(monkeypatch, capsys):
    # A transient mint failure degrades to GITHUB_TOKEN (loudly) rather than
    # crashing the run.
    monkeypatch.setattr(main.GitHubAPI, "from_app", _boom)
    cfg = SimpleNamespace(
        github_app_id="1", github_app_private_key="K",
        github_token="tok", repo_owner="o", repo_name="r",
    )
    api = main._build_github_api(cfg)
    assert api._token == "tok"
    assert "mint failed" in capsys.readouterr().err.lower()


def test_build_github_api_reraises_mint_failure_without_token(monkeypatch):
    # App-only setup (no GITHUB_TOKEN to fall back to): the failure propagates.
    monkeypatch.setattr(main.GitHubAPI, "from_app", _boom)
    cfg = SimpleNamespace(
        github_app_id="1", github_app_private_key="K",
        github_token="", repo_owner="o", repo_name="r",
    )
    with pytest.raises(RuntimeError):
        main._build_github_api(cfg)
