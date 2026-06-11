"""Escalation email: configurable sender + resilient fallback.

Regression guard for the P1 bug — the default onboarding@resend.dev sender 403s
for any real recipient, so email failed every time and the alert rode entirely on
the PR-comment fallback (which must therefore never itself be lost). The email
SEND path had no test before; this adds it.
"""

import email.message
import json
import urllib.error
from unittest import mock

from scripts.dispatcher import email_send, main
from scripts.dispatcher.config import REPO_CONFIG_PATH_ENV, load_from_env
from scripts.dispatcher.converge import CIStatus
from scripts.dispatcher.email_send import DEFAULT_FROM, EmailMessage, ResendClient
from scripts.dispatcher.redact import clear_registered, register_secret
from scripts.dispatcher.repo_config import from_mapping


def _cfg(env, tmp_path):
    base = {"GITHUB_TOKEN": "t", REPO_CONFIG_PATH_ENV: str(tmp_path / "nope.json")}
    base.update(env)
    return load_from_env(base)


# ---- sender configuration: env > repo config > "" ---------------------------
def test_email_from_env_overrides_repo_config(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text('{"email_from": "Repo <r@x.com>"}')
    cfg = load_from_env({"GITHUB_TOKEN": "t", "EMAIL_FROM": "Env <e@x.com>",
                         REPO_CONFIG_PATH_ENV: str(p)})
    assert cfg.email_from == "Env <e@x.com>"


def test_email_from_repo_config(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text('{"email_from": "Repo <r@x.com>"}')
    cfg = load_from_env({"GITHUB_TOKEN": "t", REPO_CONFIG_PATH_ENV: str(p)})
    assert cfg.email_from == "Repo <r@x.com>"


def test_email_from_defaults_empty(tmp_path):
    assert _cfg({}, tmp_path).email_from == ""


def test_repo_config_roundtrips_email_from():
    rc = from_mapping({"email_from": "A <a@b.com>"})
    assert rc.email_from == "A <a@b.com>"
    assert rc.to_dict()["email_from"] == "A <a@b.com>"


# ---- EmailMessage / ResendClient -------------------------------------------
def test_email_message_default_and_override_from():
    assert EmailMessage(to="t@x", subject="s", text="b").from_address == DEFAULT_FROM
    assert EmailMessage(to="t@x", subject="s", text="b",
                        from_address="X <x@y>").from_address == "X <x@y>"


def test_resend_send_sends_from_and_raises_on_403():
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["from"] = json.loads(req.data.decode("utf-8"))["from"]
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", email.message.Message(), None)

    with mock.patch.object(email_send.urllib.request, "urlopen", fake_urlopen):
        try:
            ResendClient("rk").send(
                EmailMessage(to="op@x", subject="s", text="b", from_address="F <f@x>"))
            assert False, "expected RuntimeError"
        except RuntimeError as exc:
            assert "403" in str(exc)
    assert captured["from"] == "F <f@x>"  # the configured sender is actually used


# ---- _send_escalation fallback behavior ------------------------------------
class _FakeAPI:
    def __init__(self, fail_post=False):
        self.comments = []
        self._fail = fail_post

    def post_comment(self, n, body):
        if self._fail:
            raise RuntimeError("HTTP 403: secondary rate limit")
        self.comments.append((n, body))
        return {"id": 1}


def _escalate(cfg, api):
    main._send_escalation(
        cfg=cfg, api=api, pr_number=7, pr_url="http://x/7", pr_title="T",
        tier="high_stakes", branch="f/x", head_sha="abc1234",
        reason_short="reviewers split", detail="d",
        reviewer_summaries={"gpt": "request_changes"},
        ci_status=CIStatus.SUCCESS, diff_summary="+1-0", workflow_run_url="http://run",
    )


def test_email_failure_falls_back_with_detail_and_hint(tmp_path):
    # email_from empty -> the misconfig that caused the bug.
    cfg = _cfg({"RESEND_API_KEY": "rk", "OPERATOR_EMAIL": "op@x.com"}, tmp_path)
    api = _FakeAPI()

    class _Boom:
        def __init__(self, key): pass
        def send(self, msg): raise RuntimeError("Resend API HTTP 403: only your own email")

    with mock.patch.object(main, "ResendClient", _Boom):
        _escalate(cfg, api)

    assert len(api.comments) == 1
    body = api.comments[0][1]
    assert "failed to send" in body and "403" in body   # real error surfaced
    assert "email_from" in body                          # actionable hint shown


def test_escalation_threads_configured_sender(tmp_path):
    cfg = _cfg({"RESEND_API_KEY": "rk", "OPERATOR_EMAIL": "op@x.com",
                "EMAIL_FROM": "Alerts <alerts@me.com>"}, tmp_path)
    seen = {}

    class _Capture:
        def __init__(self, key): pass
        def send(self, msg): seen["from"] = msg.from_address; return "id"

    with mock.patch.object(main, "ResendClient", _Capture):
        _escalate(cfg, _FakeAPI())
    assert seen["from"] == "Alerts <alerts@me.com>"


def test_post_fallback_comment_never_raises():
    # The last-resort channel must not blow up the run if GitHub also rejects it.
    api = _FakeAPI(fail_post=True)
    assert main._post_fallback_comment(api, 7, "body") is False


def test_fallback_redacts_registered_secret_locally(tmp_path):
    # _FakeAPI does NOT redact, so this proves the *local* redact() in
    # _send_escalation scrubs the error before it reaches the comment body —
    # defense in depth, not relying on post_comment's redaction.
    clear_registered()
    register_secret("MYSECRETTOKENVALUE")  # registered-only (not key-shaped)
    try:
        cfg = _cfg({"RESEND_API_KEY": "rk", "OPERATOR_EMAIL": "op@x.com"}, tmp_path)
        api = _FakeAPI()

        class _Boom:
            def __init__(self, key): pass
            def send(self, msg):
                raise RuntimeError("Resend API HTTP 401: bad key MYSECRETTOKENVALUE")

        with mock.patch.object(main, "ResendClient", _Boom):
            _escalate(cfg, api)
        body = api.comments[0][1]
        assert "MYSECRETTOKENVALUE" not in body
        assert "[REDACTED]" in body
    finally:
        clear_registered()


def test_no_email_message_names_the_missing_input(tmp_path):
    # operator set, key missing -> names RESEND_API_KEY
    api = _FakeAPI()
    _escalate(_cfg({"OPERATOR_EMAIL": "op@x.com"}, tmp_path), api)
    assert "RESEND_API_KEY not set" in api.comments[0][1]

    # key set, operator missing -> names OPERATOR_EMAIL (no longer mislabeled)
    api = _FakeAPI()
    _escalate(_cfg({"RESEND_API_KEY": "rk"}, tmp_path), api)
    assert "OPERATOR_EMAIL not set" in api.comments[0][1]

    # neither -> generic
    api = _FakeAPI()
    _escalate(_cfg({}, tmp_path), api)
    assert "no email configured" in api.comments[0][1]
