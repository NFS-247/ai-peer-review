"""Tests for scripts.dispatcher.call_google_chat (mobile escalation pings)."""

import json
from unittest import mock

import pytest

from scripts.dispatcher import call_google_chat as gc
from scripts.dispatcher import config as C


def test_card_has_header_button_and_reviewers():
    card = gc.build_escalation_card(
        project_name="TradeWatcher",
        pr_number=88,
        pr_url="https://github.com/NFS-247/StockTrader/pull/88",
        pr_title="Repoint dispatcher",
        tier="high_stakes",
        reason_short="high-stakes file changed; operator review required",
        reviewer_summaries={"claude": "request_changes", "gpt": "approve"},
    )
    c = card["cardsV2"][0]["card"]
    assert c["header"]["title"] == "TradeWatcher: PR #88 needs you"
    assert "high_stakes" in c["header"]["subtitle"]

    widgets = c["sections"][0]["widgets"]
    text = widgets[0]["textParagraph"]["text"]
    assert "claude: request_changes" in text
    assert "gpt: approve" in text

    button = widgets[1]["buttonList"]["buttons"][0]
    assert button["text"] == "Open PR #88"
    assert button["onClick"]["openLink"]["url"].endswith("/pull/88")


def test_card_has_approve_button_when_url_given():
    card = gc.build_escalation_card(
        project_name="TradeWatcher",
        pr_number=89,
        pr_url="https://github.com/NFS-247/StockTrader/pull/89",
        pr_title="x",
        tier="high_stakes",
        reason_short="r",
        reviewer_summaries={},
        approve_url="https://script.google.com/macros/s/AB/exec?repo=StockTrader&pr=89&action=approve",
    )
    buttons = card["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    texts = [b["text"] for b in buttons]
    assert texts == ["✅ Approve", "Open PR #89"]
    assert buttons[0]["onClick"]["openLink"]["url"].endswith("action=approve")


def test_card_omits_approve_button_without_url():
    card = gc.build_escalation_card(
        project_name="P", pr_number=1, pr_url="http://x/1", pr_title="t",
        tier="backend", reason_short="r", reviewer_summaries={},
    )
    buttons = card["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    assert [b["text"] for b in buttons] == ["Open PR #1"]


def test_card_has_both_buttons_when_urls_given():
    card = gc.build_escalation_card(
        project_name="P", pr_number=7, pr_url="http://x/7", pr_title="t",
        tier="high_stakes", reason_short="r", reviewer_summaries={},
        approve_url="http://x/exec?action=approve",
        approve_merge_url="http://x/exec?action=approve_merge",
    )
    buttons = card["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    assert [b["text"] for b in buttons] == ["✅ Approve", "🚀 Approve & Merge", "Open PR #7"]


def test_build_approve_url():
    base = "https://script.google.com/macros/s/AB/exec"
    url = gc.build_approve_url(base, repo="StockTrader", pr_number=89)
    assert url.startswith(base + "?")
    assert "repo=StockTrader" in url and "pr=89" in url and "action=approve" in url
    assert "sig=" not in url  # no secret -> no signature


def test_signed_url_carries_sig_and_binds_to_pr():
    base = "https://script.google.com/macros/s/AB/exec"
    secret = "s3cret"
    u89 = gc.build_approve_url(base, repo="R", pr_number=89, signing_secret=secret)
    u99 = gc.build_approve_url(base, repo="R", pr_number=99, signing_secret=secret)
    assert "sig=" in u89
    # The signature is bound to the PR number: editing pr= invalidates it.
    sig89 = u89.split("sig=")[1]
    sig99 = u99.split("sig=")[1]
    assert sig89 != sig99
    # And it matches a recomputation over repo:pr:action.
    assert sig89 == gc.sign_action(secret, repo="R", pr_number=89, action="approve")


def test_sign_action_distinguishes_action():
    s = "k"
    assert gc.sign_action(s, repo="R", pr_number=1, action="approve") != \
        gc.sign_action(s, repo="R", pr_number=1, action="approve_merge")


def test_merge_url_uses_merge_action():
    url = gc.build_approve_url(
        "https://x/exec", repo="R", pr_number=7, action="approve_merge", signing_secret="k"
    )
    assert "action=approve_merge" in url


def test_build_approve_url_appends_with_existing_query():
    base = "https://script.google.com/macros/s/AB/exec?token=abc"
    url = gc.build_approve_url(base, repo="R", pr_number=5)
    assert "?token=abc&" in url
    assert url.count("?") == 1


def test_build_approve_url_empty_base_returns_empty():
    assert gc.build_approve_url("", repo="R", pr_number=5) == ""


def test_ready_card_has_header_and_merge_button():
    card = gc.build_ready_card(
        project_name="Canary", pr_number=42,
        pr_url="https://github.com/NFS-247/Canary/pull/42",
        pr_title="Add feature", tier="backend",
        approve_merge_url="https://x/exec?action=approve_merge",
    )
    c = card["cardsV2"][0]["card"]
    assert c["header"]["title"] == "Canary: PR #42 ready to merge"
    assert "backend" in c["header"]["subtitle"]
    buttons = c["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    assert [b["text"] for b in buttons] == ["🚀 Approve & Merge", "Open PR #42"]
    assert buttons[0]["onClick"]["openLink"]["url"].endswith("action=approve_merge")


def test_ready_card_without_merge_url_only_open():
    card = gc.build_ready_card(
        project_name="P", pr_number=1, pr_url="http://x/1", pr_title="t", tier="routine",
    )
    buttons = card["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    assert [b["text"] for b in buttons] == ["Open PR #1"]


def test_ready_card_escapes_html_in_title():
    card = gc.build_ready_card(
        project_name="P", pr_number=1, pr_url="http://x/1",
        pr_title="fix <b> & x", tier="routine",
    )
    text = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    # The injected <b> from the title is escaped; "fix <b>" would only appear
    # unescaped if the title weren't escaped.
    assert "&lt;b&gt;" in text and "&amp;" in text
    assert "fix <b>" not in text


def test_card_escapes_html_in_title():
    card = gc.build_escalation_card(
        project_name="P",
        pr_number=1,
        pr_url="http://x/1",
        pr_title="fix <script> & stuff",
        tier="backend",
        reason_short="r",
        reviewer_summaries={},
    )
    text = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "&lt;script&gt;" in text
    assert "&amp;" in text
    assert "<script>" not in text


def test_format_spend_breakdown_sorted_desc():
    s = gc.format_spend_breakdown({"gpt": 0.06, "claude": 0.41, "gemini": 0.10})
    assert s == "claude $0.41 · gemini $0.10 · gpt $0.06"


def test_format_spend_breakdown_empty_is_blank():
    assert gc.format_spend_breakdown(None) == ""
    assert gc.format_spend_breakdown({}) == ""


def test_escalation_card_shows_spend_breakdown_when_given():
    card = gc.build_escalation_card(
        project_name="P", pr_number=5, pr_url="http://x/5", pr_title="t",
        tier="high_stakes", reason_short="24-hour dispatcher spend ceiling reached",
        reviewer_summaries={}, spend_breakdown={"claude": 0.41, "gpt": 0.06},
    )
    text = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "24h spend:" in text
    assert "claude $0.41" in text and "gpt $0.06" in text


def test_escalation_card_omits_spend_line_without_breakdown():
    card = gc.build_escalation_card(
        project_name="P", pr_number=5, pr_url="http://x/5", pr_title="t",
        tier="backend", reason_short="r", reviewer_summaries={},
    )
    text = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "24h spend:" not in text


def test_budget_warning_card_shows_breakdown():
    card = gc.build_budget_warning_card(
        project_name="P", spent_usd=16.5, ceiling_usd=20.0,
        breakdown={"gemini": 9.0, "claude": 5.0, "gpt": 2.5},
    )
    text = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "Where it's going:" in text
    assert "gemini $9.00" in text  # the dominant model is named first


def test_budget_escalation_card_omits_approve_buttons():
    # THE fix: a spend-ceiling stop must NOT offer Approve / Approve & Merge — the
    # bot paused on money (maybe with zero reviews), so approving could merge an
    # unreviewed PR. Buttons match the message: Increase limit + Open PR only.
    card = gc.build_budget_escalation_card(
        project_name="TradeWatcher", pr_number=133,
        pr_url="https://github.com/NFS-247/StockTrader/pull/133",
        spent_usd=15.68, ceiling_usd=15.0,
        breakdown={"claude": 13.11, "gpt": 1.86, "gemini": 0.71},
        increase_url="https://github.com/NFS-247/StockTrader/blob/HEAD/.peer-review.json",
    )
    buttons = card["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    texts = [b["text"] for b in buttons]
    assert texts == ["💵 Increase limit", "Open PR #133"]
    assert "Approve" not in " ".join(texts)
    assert buttons[0]["onClick"]["openLink"]["url"].endswith("/.peer-review.json")


def test_budget_escalation_card_shows_spend_and_ceiling():
    card = gc.build_budget_escalation_card(
        project_name="P", pr_number=1, pr_url="http://x/1",
        spent_usd=15.68, ceiling_usd=15.0,
        breakdown={"claude": 13.11, "gpt": 1.86},
    )
    c = card["cardsV2"][0]["card"]
    text = c["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "$15.68" in text and "$15.00" in text
    assert "24h spend:" in text and "claude $13.11" in text
    assert "$15.68 / $15.00" in c["header"]["subtitle"]


def test_budget_escalation_card_open_only_without_increase_url():
    card = gc.build_budget_escalation_card(
        project_name="P", pr_number=2, pr_url="http://x/2",
        spent_usd=5.0, ceiling_usd=5.0,
    )
    buttons = card["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    assert [b["text"] for b in buttons] == ["Open PR #2"]


def test_build_increase_limit_url():
    assert gc.build_increase_limit_url("NFS-247/StockTrader") == (
        "https://github.com/NFS-247/StockTrader/blob/HEAD/.peer-review.json"
    )
    assert gc.build_increase_limit_url("") == ""


def test_send_requires_url():
    with pytest.raises(ValueError):
        gc.send_chat_message("", {"text": "hi"})


def test_send_posts_json_to_webhook():
    captured = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["content_type"] = req.headers.get("Content-type")
        return _Resp()

    with mock.patch.object(gc.urllib.request, "urlopen", fake_urlopen):
        gc.send_chat_message("https://chat.googleapis.com/v1/spaces/AAA/messages?key=k&token=t",
                             {"text": "hello"})

    assert captured["url"].startswith("https://chat.googleapis.com/")
    assert captured["method"] == "POST"
    assert captured["body"] == {"text": "hello"}
    assert "application/json" in captured["content_type"]


def test_config_loads_webhook_from_env():
    cfg = C.load_from_env({
        "GITHUB_TOKEN": "t",
        "GOOGLE_CHAT_WEBHOOK_URL": "https://chat.googleapis.com/v1/spaces/x/messages?key=k ",
    })
    # secret() strips surrounding whitespace.
    assert cfg.google_chat_webhook_url == "https://chat.googleapis.com/v1/spaces/x/messages?key=k"


def test_config_webhook_absent_is_none():
    cfg = C.load_from_env({"GITHUB_TOKEN": "t"})
    assert cfg.google_chat_webhook_url is None
    assert cfg.approve_webapp_url is None


def test_config_loads_approve_url_from_env():
    cfg = C.load_from_env({
        "GITHUB_TOKEN": "t",
        "APPROVE_WEBAPP_URL": "https://script.google.com/macros/s/AB/exec",
        "APPROVE_SIGNING_SECRET": "sec",
    })
    assert cfg.approve_webapp_url == "https://script.google.com/macros/s/AB/exec"
    assert cfg.approve_signing_secret == "sec"
