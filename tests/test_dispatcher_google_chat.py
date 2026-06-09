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
