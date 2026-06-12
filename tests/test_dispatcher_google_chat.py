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
        increase_url="https://github.com/NFS-247/StockTrader/edit/HEAD/.peer-review.json",
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
        "https://github.com/NFS-247/StockTrader/edit/HEAD/.peer-review.json"
    )
    # malformed 'owner/name' -> "" (never an "owner/" or "/name" link)
    for bad in ("", "owner-only", "owner/", "/name"):
        assert gc.build_increase_limit_url(bad) == ""


def test_budget_escalation_card_includes_pr_title():
    card = gc.build_budget_escalation_card(
        project_name="P", pr_number=1, pr_url="http://x/1",
        spent_usd=5.0, ceiling_usd=5.0, pr_title="Repoint dispatcher <x>",
    )
    text = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "Repoint dispatcher" in text          # operator context preserved
    assert "&lt;x&gt;" in text                    # and HTML-escaped


def test_budget_escalation_card_no_spend_line_without_breakdown():
    # 24h breakdown unavailable -> show the total but NOT a misleading per-model line.
    card = gc.build_budget_escalation_card(
        project_name="P", pr_number=1, pr_url="http://x/1",
        spent_usd=15.68, ceiling_usd=15.0, breakdown=None,
    )
    text = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "$15.68" in text
    assert "24h spend:" not in text


def test_disagreement_card_shows_split_and_override_action():
    card = gc.build_disagreement_card(
        project_name="NFS Central", pr_number=91,
        pr_url="https://github.com/NFS-247/nfs-central/pull/91",
        pr_title="Vendor Intelligence PR 1",
        tier="high_stakes", reason_short="hard round cap reached",
        reviewer_summaries={"claude": "request_changes", "gpt": "request_changes",
                            "gemini": "approve"},
        approve_url="https://script.google.com/macros/s/AB/exec?action=approve",
    )
    c = card["cardsV2"][0]["card"]
    assert c["header"]["title"] == "NFS Central: PR #91 — reviewers split"
    text = c["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "Reviewers couldn't agree" in text
    assert "✅ approve: gemini" in text
    assert "✋ want changes: claude, gpt" in text       # who's blocking, surfaced
    texts = [b["text"] for b in c["sections"][0]["widgets"][1]["buttonList"]["buttons"]]
    assert texts == ["✅ Approve & mark ready", "Open PR #91"]
    assert "Merge" not in " ".join(texts)               # no one-tap merge on a contested PR


def test_disagreement_card_omits_approve_without_url():
    card = gc.build_disagreement_card(
        project_name="P", pr_number=1, pr_url="http://x/1", pr_title="t",
        tier="high_stakes", reason_short="reviewers disagreed past round budget",
        reviewer_summaries={"claude": "request_changes", "gpt": "approve"},
    )
    buttons = card["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    assert [b["text"] for b in buttons] == ["Open PR #1"]
    # No webapp -> the body keeps the typed-command instructions so the operator
    # still knows how to act from the PR itself.
    text = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "OPERATOR INVESTIGATE" in text


def test_disagreement_card_one_tap_send_back_and_block():
    # With the approve-webapp configured, the card offers one-tap Send back
    # (INVESTIGATE) and Block alongside Approve — no typed OPERATOR commands.
    card = gc.build_disagreement_card(
        project_name="TradeWatcher", pr_number=140,
        pr_url="https://github.com/NFS-247/StockTrader/pull/140",
        pr_title="Collapse /ui", tier="high_stakes",
        reason_short="high-stakes dissent on first review",
        reviewer_summaries={"gemini": "approve", "claude": "request_changes",
                            "gpt": "request_changes"},
        approve_url="https://script.google.com/exec?action=approve&sig=a",
        investigate_url="https://script.google.com/exec?action=investigate&sig=b",
        block_url="https://script.google.com/exec?action=block&sig=c",
    )
    c = card["cardsV2"][0]["card"]
    btns = c["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    assert [b["text"] for b in btns] == [
        "✅ Approve & mark ready", "🔄 Send back (1 more round)", "✋ Block", "Open PR #140",
    ]
    assert btns[1]["onClick"]["openLink"]["url"].endswith("action=investigate&sig=b")
    assert btns[2]["onClick"]["openLink"]["url"].endswith("action=block&sig=c")
    # body steers to the buttons, not typed commands
    text = c["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "Tap a button below" in text
    assert "OPERATOR INVESTIGATE" not in text


def test_disagreement_card_escapes_html_in_title():
    card = gc.build_disagreement_card(
        project_name="P", pr_number=1, pr_url="http://x/1",
        pr_title="fix <script> & x", tier="backend",
        reason_short="r", reviewer_summaries={},
    )
    text = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "&lt;script&gt;" in text and "<script>" not in text


def test_disagreement_card_buckets_non_verdict_reviewers():
    # A reviewer that errored / only commented must NOT show as 'want changes' —
    # it goes to a separate bucket so the override decision isn't misled.
    card = gc.build_disagreement_card(
        project_name="P", pr_number=5, pr_url="http://x/5", pr_title="t",
        tier="high_stakes", reason_short="hard round cap reached",
        reviewer_summaries={"claude": "request_changes", "gpt": "approve",
                            "gemini": "error"},
    )
    text = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "✋ want changes: claude" in text
    assert "✅ approve: gpt" in text
    assert "no clear verdict: gemini" in text          # errored reviewer, not a blocker
    assert "want changes: claude, gemini" not in text  # gemini is NOT lumped in


def test_disagreement_card_split_is_sorted_regardless_of_input_order():
    card = gc.build_disagreement_card(
        project_name="P", pr_number=1, pr_url="http://x/1", pr_title="t",
        tier="high_stakes", reason_short="r",
        reviewer_summaries={"gpt": "request_changes", "claude": "request_changes"},
    )
    text = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "✋ want changes: claude, gpt" in text       # sorted, not input order


def test_disagreement_card_handles_round_suffix_in_verdict():
    # Production summaries are "<verdict> (round N)" — the leading token is matched,
    # so the round suffix doesn't shove a blocker into "no clear verdict".
    card = gc.build_disagreement_card(
        project_name="P", pr_number=1, pr_url="http://x/1", pr_title="t",
        tier="high_stakes", reason_short="hard round cap reached",
        reviewer_summaries={"claude": "request_changes (round 6)",
                            "gpt": "request_changes (round 6)",
                            "gemini": "approve (round 6)"},
    )
    text = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "✋ want changes: claude, gpt" in text
    assert "✅ approve: gemini" in text
    assert "no clear verdict" not in text   # the (round N) suffix didn't misbucket


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


def test_escalation_card_plain_lead_and_clean_subtitle_with_trigger():
    # With the trigger, the card leads with a human sentence and drops the engine
    # jargon from the subtitle (the operator's complaint: "a required reviewer
    # could not be reached" meant nothing to them).
    card = gc.build_escalation_card(
        project_name="NFS Central", pr_number=95, pr_url="http://x/95",
        pr_title="Vendor Intelligence: sourcing board", tier="high_stakes",
        reason_short="a required reviewer could not be reached",
        reviewer_summaries={"gemini": "API failure this round"},
        trigger="required_reviewer_unavailable",
    )
    c = card["cardsV2"][0]["card"]
    text = c["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "didn't answer this round" in text                       # plain lead
    assert "a required reviewer could not be reached" not in text   # jargon gone from body
    assert c["header"]["subtitle"] == "tier: high_stakes"           # and from subtitle


def test_escalation_card_keeps_literal_reason_without_trigger():
    # Back-compat: callers that don't pass a trigger keep the old "Why: <reason>"
    # body + the reason in the subtitle, unchanged.
    card = gc.build_escalation_card(
        project_name="P", pr_number=1, pr_url="http://x/1", pr_title="t",
        tier="backend", reason_short="CI is persistently failing",
        reviewer_summaries={},
    )
    c = card["cardsV2"][0]["card"]
    text = c["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "<b>Why:</b> CI is persistently failing" in text
    assert c["header"]["subtitle"] == "tier: backend · CI is persistently failing"


def test_escalation_card_unknown_trigger_keeps_literal_reason():
    # An UNRECOGNIZED trigger must NOT hide the precise reason behind a generic
    # fallback — keep the literal "Why: <reason>" body + reason in the subtitle so
    # an unexpected state keeps its diagnostics (gpt's review note).
    card = gc.build_escalation_card(
        project_name="P", pr_number=1, pr_url="http://x/1", pr_title="t",
        tier="backend", reason_short="something unexpected happened",
        reviewer_summaries={}, trigger="not_a_real_trigger",
    )
    c = card["cardsV2"][0]["card"]
    text = c["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "<b>Why:</b> something unexpected happened" in text
    assert c["header"]["subtitle"] == "tier: backend · something unexpected happened"


def test_escalation_card_links_to_down_reviewers_provider_console():
    # The button that makes sense when an AI is down is a jump straight to THAT
    # provider's console — gemini -> AI Studio — placed before "Open PR".
    card = gc.build_escalation_card(
        project_name="NFS Central", pr_number=95, pr_url="http://x/95",
        pr_title="t", tier="high_stakes",
        reason_short="a required reviewer could not be reached",
        reviewer_summaries={"gemini": "API failure this round"},
        trigger="required_reviewer_unavailable",
        unavailable_reviewers=("gemini",),
    )
    buttons = card["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"]
    assert [b["text"] for b in buttons] == ["🔧 Check Gemini (AI Studio)", "Open PR #95"]
    assert buttons[0]["onClick"]["openLink"]["url"] == "https://aistudio.google.com/"


def test_escalation_card_skips_unknown_provider_button():
    # An unrecognized reviewer name gets NO button (never a wrong link), just Open PR.
    card = gc.build_escalation_card(
        project_name="P", pr_number=1, pr_url="http://x/1", pr_title="t",
        tier="high_stakes", reason_short="r", reviewer_summaries={},
        trigger="required_reviewer_unavailable", unavailable_reviewers=("mystery-bot",),
    )
    texts = [b["text"] for b in
             card["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"]]
    assert texts == ["Open PR #1"]


def test_plain_escalation_lead_known_and_unknown():
    assert "protected files" in gc.plain_escalation_lead("high_stakes_auto")
    # blank or unknown both resolve to the same safe fallback
    assert gc.plain_escalation_lead("") == gc.plain_escalation_lead("nope")


def test_disagreement_card_all_approved_not_split_offers_merge():
    # Everyone approved but it didn't auto-converge (hard round cap with CI pending
    # / head moved): the card must NOT say "split", and it SHOULD offer merge.
    card = gc.build_disagreement_card(
        project_name="P", pr_number=111, pr_url="http://x/111", pr_title="T",
        tier="high_stakes", reason_short="hard round cap reached",
        reviewer_summaries={"claude": "approve (round 6)", "gpt": "approve (round 6)",
                            "gemini": "approve (round 6)"},
        approve_url="http://a", approve_merge_url="http://m",
        investigate_url="http://i", block_url="http://b",
    )
    c = card["cardsV2"][0]["card"]
    assert "reviewers split" not in c["header"]["title"]
    assert "approved — needs you" in c["header"]["title"]
    text = c["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "All reviewers approved" in text and "couldn't agree" not in text
    texts = [b["text"] for b in c["sections"][0]["widgets"][1]["buttonList"]["buttons"]]
    assert "🚀 Approve & Merge" in texts                 # the fix
    assert "✅ Approve & mark ready" in texts


def test_disagreement_card_contested_stays_split_no_merge():
    # Genuine dissent: keep the "split" framing and WITHHOLD one-tap merge, even
    # when an approve_merge link is available.
    card = gc.build_disagreement_card(
        project_name="P", pr_number=9, pr_url="http://x/9", pr_title="T",
        tier="backend", reason_short="hard round cap reached",
        reviewer_summaries={"claude": "request_changes (round 6)", "gpt": "approve (round 6)"},
        approve_url="http://a", approve_merge_url="http://m",
    )
    c = card["cardsV2"][0]["card"]
    assert "reviewers split" in c["header"]["title"]
    text = c["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "Reviewers couldn't agree" in text and "✋ want changes: claude" in text
    texts = [b["text"] for b in c["sections"][0]["widgets"][1]["buttonList"]["buttons"]]
    assert "🚀 Approve & Merge" not in texts             # never one-tap-merge a contested PR


def test_escalation_card_send_back_block_buttons():
    # A head-lock / gut-check escalation with signed investigate+block links gets
    # one-tap "Send back" and "Block" buttons, so the operator never has to open
    # the PR to type a command.
    card = gc.build_escalation_card(
        project_name="P", pr_number=107, pr_url="http://x/107", pr_title="T",
        tier="high_stakes", reason_short="r", reviewer_summaries={"claude": "approve"},
        approve_url="http://a", investigate_url="http://i", block_url="http://b",
    )
    sec = card["cardsV2"][0]["card"]["sections"][0]
    texts = [btn["text"] for btn in sec["widgets"][1]["buttonList"]["buttons"]]
    assert "🔄 Send back (1 more round)" in texts
    assert "✋ Block" in texts
    body = sec["widgets"][0]["textParagraph"]["text"]
    # both actions are buttons now, so the body shouldn't tell you to open the PR
    assert "Tap a button below." in body
    assert "open the PR for" not in body


def test_escalation_card_points_at_pr_for_missing_button_actions():
    # Approve only (no investigate/block links) -> the body still points at the PR
    # for the actions that aren't buttons.
    card = gc.build_escalation_card(
        project_name="P", pr_number=9, pr_url="http://x/9", pr_title="T",
        tier="high_stakes", reason_short="r", reviewer_summaries={},
        approve_url="http://a",
    )
    sec = card["cardsV2"][0]["card"]["sections"][0]
    texts = [btn["text"] for btn in sec["widgets"][1]["buttonList"]["buttons"]]
    assert "🔄 Send back (1 more round)" not in texts
    body = sec["widgets"][0]["textParagraph"]["text"]
    assert "INVESTIGATE" in body and "BLOCK" in body


def test_disagreement_card_error_reviewer_not_all_approved_no_merge():
    # gpt's #18 concern: no dissent ≠ all approved. A reviewer that errored (or
    # returned no clear verdict) means the panel has a hole — the card must NOT
    # claim unanimity and must NOT offer one-tap merge.
    card = gc.build_disagreement_card(
        project_name="P", pr_number=111, pr_url="http://x/111", pr_title="T",
        tier="high_stakes", reason_short="hard round cap reached",
        reviewer_summaries={"claude": "approve (round 6)", "gemini": "approve (round 6)",
                            "gpt": "API failure this round"},
        approve_url="http://a", approve_merge_url="http://m",
        investigate_url="http://i", block_url="http://b",
    )
    c = card["cardsV2"][0]["card"]
    assert "reviews incomplete — needs you" in c["header"]["title"]
    assert "reviewers split" not in c["header"]["title"]   # nobody dissented
    text = c["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "All reviewers approved" not in text
    assert "❔ no clear verdict: gpt" in text
    texts = [b["text"] for b in c["sections"][0]["widgets"][1]["buttonList"]["buttons"]]
    assert "🚀 Approve & Merge" not in texts               # incomplete panel: no one-tap merge
    assert "✅ Approve & mark ready" in texts              # explicit override stays available


def test_disagreement_card_missing_expected_reviewer_no_merge():
    # A reviewer that never reported at all is invisible in the summaries — the
    # expected panel makes the hole visible: bucketed "no clear verdict", no
    # unanimity claim, no one-tap merge.
    card = gc.build_disagreement_card(
        project_name="P", pr_number=111, pr_url="http://x/111", pr_title="T",
        tier="high_stakes", reason_short="hard round cap reached",
        reviewer_summaries={"claude": "approve (round 6)", "gemini": "approve (round 6)"},
        expected_reviewers=("claude", "gpt", "gemini"),
        approve_url="http://a", approve_merge_url="http://m",
    )
    c = card["cardsV2"][0]["card"]
    assert "reviews incomplete — needs you" in c["header"]["title"]
    text = c["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "All reviewers approved" not in text
    assert "❔ no clear verdict: gpt" in text
    texts = [b["text"] for b in c["sections"][0]["widgets"][1]["buttonList"]["buttons"]]
    assert "🚀 Approve & Merge" not in texts


def test_disagreement_card_full_expected_panel_still_offers_merge():
    # The expected-panel seeding must not break the true all-approved case.
    card = gc.build_disagreement_card(
        project_name="P", pr_number=111, pr_url="http://x/111", pr_title="T",
        tier="high_stakes", reason_short="hard round cap reached",
        reviewer_summaries={"claude": "approve (round 6)", "gpt": "approve (round 6)",
                            "gemini": "approve (round 6)"},
        expected_reviewers=("claude", "gpt", "gemini"),
        approve_url="http://a", approve_merge_url="http://m",
    )
    c = card["cardsV2"][0]["card"]
    assert "approved — needs you" in c["header"]["title"]
    texts = [b["text"] for b in c["sections"][0]["widgets"][1]["buttonList"]["buttons"]]
    assert "🚀 Approve & Merge" in texts


def test_disagreement_card_fallback_prose_override_only_when_contested():
    # No webapp links -> typed-command prose. Approving over a dissent is an
    # override and must SAY so; with no dissent there's nothing to override.
    contested = gc.build_disagreement_card(
        project_name="P", pr_number=9, pr_url="http://x/9", pr_title="T",
        tier="backend", reason_short="r",
        reviewer_summaries={"claude": "request_changes (round 2)", "gpt": "approve (round 2)"},
    )
    text = contested["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "override" in text and "read the concerns" in text
    uncontested = gc.build_disagreement_card(
        project_name="P", pr_number=9, pr_url="http://x/9", pr_title="T",
        tier="backend", reason_short="r",
        reviewer_summaries={"claude": "approve (round 2)", "gpt": "approve (round 2)"},
    )
    text = uncontested["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["textParagraph"]["text"]
    assert "override" not in text
