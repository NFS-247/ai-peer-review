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


def test_budget_spike_escalation_uses_budget_card_not_approve(tmp_path):
    # End-to-end: a DAILY_COST_SPIKE routes to the budget card — Increase limit +
    # Open PR, NEVER Approve/Approve&Merge (the bot stopped on money, not a review).
    from scripts.dispatcher.escalation import EscalationTrigger

    cfg = _cfg({
        "GOOGLE_CHAT_WEBHOOK_URL": "https://chat.googleapis.com/v1/spaces/x/messages?key=k",
        "GITHUB_REPOSITORY": "NFS-247/StockTrader",
        "GITHUB_REPOSITORY_OWNER": "NFS-247",
        # approve web app IS configured — proving the budget card omits Approve by
        # choice, not just because no approve URL was available.
        "APPROVE_WEBAPP_URL": "https://script.google.com/macros/s/AB/exec",
    }, tmp_path)
    sent = {}

    with mock.patch.object(main, "send_chat_message", lambda url, card: sent.update(card=card)):
        main._send_escalation(
            cfg=cfg, api=_FakeAPI(), pr_number=133,
            pr_url="https://github.com/NFS-247/StockTrader/pull/133", pr_title="T",
            tier="high_stakes", branch="f/x", head_sha="abc1234",
            reason_short="24-hour dispatcher spend ceiling reached", detail="d",
            reviewer_summaries={}, ci_status=CIStatus.SUCCESS,
            diff_summary="+1-0", workflow_run_url="http://run",
            spend_breakdown={"claude": 13.11, "gpt": 1.86, "gemini": 0.71},
            spent_usd=15.68,
            trigger=EscalationTrigger.DAILY_COST_SPIKE,
        )

    section = sent["card"]["cardsV2"][0]["card"]["sections"][0]
    buttons = section["widgets"][1]["buttonList"]["buttons"]
    texts = [b["text"] for b in buttons]
    assert "Approve" not in " ".join(texts)          # the fix
    assert "💵 Increase limit" in texts
    assert any(
        "NFS-247/StockTrader/edit/HEAD" in b["onClick"]["openLink"]["url"]
        for b in buttons if "Increase" in b["text"]
    )


def test_budget_card_sums_breakdown_when_no_total(tmp_path):
    # spent_usd not passed -> _send_escalation sums the breakdown for the card.
    from scripts.dispatcher.escalation import EscalationTrigger

    cfg = _cfg({
        "GOOGLE_CHAT_WEBHOOK_URL": "https://chat.googleapis.com/v1/spaces/x/messages?key=k",
        "GITHUB_REPOSITORY": "NFS-247/StockTrader",
    }, tmp_path)
    sent = {}
    with mock.patch.object(main, "send_chat_message", lambda url, card: sent.update(card=card)):
        main._send_escalation(
            cfg=cfg, api=_FakeAPI(), pr_number=1, pr_url="http://x/1", pr_title="T",
            tier="high_stakes", branch="f/x", head_sha="abc1234",
            reason_short="24-hour dispatcher spend ceiling reached", detail="d",
            reviewer_summaries={}, ci_status=CIStatus.SUCCESS,
            diff_summary="+1-0", workflow_run_url="http://run",
            spend_breakdown={"claude": 13.11, "gpt": 1.86, "gemini": 0.71},
            spent_usd=None,                       # no total -> must sum to 15.68
            trigger=EscalationTrigger.DAILY_COST_SPIKE,
        )
    card = sent["card"]["cardsV2"][0]["card"]
    assert "$15.68" in card["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "$15.68" in card["header"]["subtitle"]


def test_non_budget_escalation_keeps_approve_buttons(tmp_path):
    # Regression: a NON-budget escalation with an approve web app still offers
    # Approve / Approve & Merge — the trigger branch didn't affect other types.
    cfg = _cfg({
        "GOOGLE_CHAT_WEBHOOK_URL": "https://chat.googleapis.com/v1/spaces/x/messages?key=k",
        "GITHUB_REPOSITORY": "NFS-247/StockTrader",
        "APPROVE_WEBAPP_URL": "https://script.google.com/macros/s/AB/exec",
    }, tmp_path)
    sent = {}
    with mock.patch.object(main, "send_chat_message", lambda url, card: sent.update(card=card)):
        main._send_escalation(
            cfg=cfg, api=_FakeAPI(), pr_number=9, pr_url="http://x/9", pr_title="T",
            tier="high_stakes", branch="f/x", head_sha="abc1234",
            reason_short="high-stakes file changed; operator review required", detail="d",
            reviewer_summaries={"claude": "approve"}, ci_status=CIStatus.SUCCESS,
            diff_summary="+1-0", workflow_run_url="http://run",
            # no trigger -> the standard approval card
        )
    section = sent["card"]["cardsV2"][0]["card"]["sections"][0]
    texts = [b["text"] for b in section["widgets"][1]["buttonList"]["buttons"]]
    assert "✅ Approve" in texts and "🚀 Approve & Merge" in texts


def test_budget_email_omits_operator_approve():
    # A budget stop's email/comment must NOT present OPERATOR APPROVE — reviews may
    # not have run, so approving would mark an unreviewed PR ready (matches the
    # Chat card, which drops Approve). Other escalations keep the full command set.
    common = dict(
        project_name="P", pr_number=1, pr_url="http://x/1", pr_title="T",
        tier="high_stakes", branch="f/x", reason_short="24h spend ceiling reached",
        detail="d", reviewer_summaries={}, ci_status="success", head_sha="abc",
        diff_summary="+1-0", workflow_run_url="http://run",
    )
    budget = email_send.build_escalation_email(**common, is_budget_stop=True).text
    normal = email_send.build_escalation_email(**common, is_budget_stop=False).text
    assert "OPERATOR APPROVE" not in budget
    assert "daily_cost_ceiling_usd" in budget          # points at raising the ceiling
    assert "OPERATOR APPROVE" in normal                # unchanged for normal escalations


def test_budget_pr_comment_fallback_omits_operator_approve(tmp_path):
    # No email configured -> the durable channel is the PR comment (= body_text).
    # For a budget stop it must also omit OPERATOR APPROVE.
    from scripts.dispatcher.escalation import EscalationTrigger

    cfg = _cfg({"GITHUB_REPOSITORY": "NFS-247/StockTrader"}, tmp_path)
    api = _FakeAPI()
    main._send_escalation(
        cfg=cfg, api=api, pr_number=1, pr_url="http://x/1", pr_title="T",
        tier="high_stakes", branch="f/x", head_sha="abc1234",
        reason_short="24h spend ceiling reached", detail="d",
        reviewer_summaries={}, ci_status=CIStatus.SUCCESS,
        diff_summary="+1-0", workflow_run_url="http://run",
        trigger=EscalationTrigger.DAILY_COST_SPIKE,
    )
    assert api.comments, "expected a durable PR-comment fallback"
    assert "OPERATOR APPROVE" not in api.comments[0][1]


def test_disagreement_trigger_uses_disagreement_card(tmp_path):
    # A reviewers-couldn't-converge trigger (hard round cap) routes to the
    # disagreement card: the split + Approve-to-override, not the generic approval
    # card and not a one-tap merge.
    from scripts.dispatcher.escalation import EscalationTrigger

    cfg = _cfg({
        "GOOGLE_CHAT_WEBHOOK_URL": "https://chat.googleapis.com/v1/spaces/x/messages?key=k",
        "GITHUB_REPOSITORY": "NFS-247/nfs-central",
        "APPROVE_WEBAPP_URL": "https://script.google.com/macros/s/AB/exec",
    }, tmp_path)
    sent = {}
    with mock.patch.object(main, "send_chat_message", lambda url, card: sent.update(card=card)):
        main._send_escalation(
            cfg=cfg, api=_FakeAPI(), pr_number=91, pr_url="http://x/91", pr_title="T",
            tier="high_stakes", branch="f/x", head_sha="abc1234",
            reason_short="hard round cap reached", detail="d",
            reviewer_summaries={"claude": "request_changes", "gpt": "request_changes",
                                "gemini": "approve"},
            ci_status=CIStatus.SUCCESS, diff_summary="+1-0", workflow_run_url="http://run",
            trigger=EscalationTrigger.HARD_ROUND_CAP,
        )
    c = sent["card"]["cardsV2"][0]["card"]
    assert "reviewers split" in c["header"]["title"]
    text = c["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "Reviewers couldn't agree" in text and "✋ want changes: claude, gpt" in text
    texts = [b["text"] for b in c["sections"][0]["widgets"][1]["buttonList"]["buttons"]]
    assert "✅ Approve & mark ready" in texts            # operator can override
    assert "🚀 Approve & Merge" not in texts              # not one-tap merge a contested PR
