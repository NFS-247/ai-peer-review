"""Google Chat incoming-webhook sender for escalation notifications.

An optional push channel so the operator is pinged on their phone the moment a
PR needs a decision. Configured via the GOOGLE_CHAT_WEBHOOK_URL secret.

This is an ADDITIONAL channel layered on top of the email/PR-comment path in
main._send_escalation. The PR comment remains the guaranteed durable record, so
a Chat delivery failure can never lose a notification — it is best-effort.

Pure stdlib (urllib), no dependencies, consistent with the rest of the package.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping


def _esc(text: str) -> str:
    """Escape the small set of chars Google Chat treats as markup in text widgets."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_spend_breakdown(breakdown: Mapping[str, float] | None) -> str:
    """Render a per-provider spend map as 'claude $0.41 · gpt $0.06', cost-desc.

    Returns "" for an empty/None map so callers can omit the line entirely.
    Reviewer names are provider identifiers (claude/gpt/gemini), already safe,
    but escaped defensively in case the map is ever extended.
    """
    if not breakdown:
        return ""
    items = sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)
    return " · ".join(f"{_esc(str(name))} ${amount:.2f}" for name, amount in items)


# Plain-language lead for the generic escalation card, keyed by EscalationTrigger
# value (passed as a raw string so this presentation module never imports the
# escalation logic). The operator sees a human sentence — "a reviewer couldn't
# finish, take a look" — instead of the engine's internal reason code. The precise
# reason_short is still kept on the email + PR comment as the durable record.
_PLAIN_LEAD: dict[str, str] = {
    "required_reviewer_unavailable":
        "A reviewer's API didn't answer this round, so this PR can't finish on "
        "its own. Check that provider below, then send it back another round.",
    "api_outage":
        "A reviewer's AI service has been down for a while, so this PR is on hold.",
    "ci_persistent_failure":
        "The automated tests keep failing and the fixes aren't sticking — this "
        "one needs you.",
    "high_stakes_auto":
        "This PR changes protected files, so it needs your sign-off before it can "
        "merge — even though the reviewers are happy.",
    "suspicious_unanimous":
        "Every reviewer approved this fast on a high-stakes change. Worth a quick "
        "gut-check before it goes.",
    # Disagreement / cost leads below are consumed by the EMAIL + durable
    # PR-comment (the Chat side routes these triggers to their own richer cards);
    # they give that plaintext channel the same human first line.
    "hard_round_cap":
        "The reviewers used up their rounds without fully agreeing — your call to "
        "break the tie: approve it, send it back, or block it.",
    "cost_spike":
        "This PR spent its review budget without the reviewers agreeing — your "
        "call before it spends any more.",
    "daily_cost_spike":
        "Reviews are paused — the project hit its daily AI-review spending limit. "
        "Raise the limit to resume.",
}
_PLAIN_LEAD_FALLBACK = "This PR is stuck and waiting on your decision."


def plain_escalation_lead(trigger: str) -> str:
    """Human one-liner for an escalation trigger; safe fallback for unknowns.

    The fallback is for direct/defensive callers. build_escalation_card does NOT
    rely on it: it swaps in this lead only for a RECOGNIZED trigger (``trigger in
    _PLAIN_LEAD``) and otherwise keeps the literal ``reason_short``, so an
    unexpected trigger never hides its diagnostics behind a generic line.
    """
    return _PLAIN_LEAD.get(trigger, _PLAIN_LEAD_FALLBACK)


def known_plain_lead(trigger: str) -> str:
    """Plain lead for a RECOGNIZED trigger, else ``""`` (no generic fallback).

    Unlike ``plain_escalation_lead``, returns ``""`` for a blank/unknown trigger
    so a caller can fall back to the engine's literal reason — keeping the real
    diagnostics visible — instead of masking it with a generic sentence. The
    email/PR-comment builder uses this: it carries every trigger and wants the
    plain lead only when we actually have human copy for it.
    """
    return _PLAIN_LEAD.get(trigger, "")


# When a reviewer's own API is what's down, the button that makes sense isn't
# "Open PR" — it's a jump straight to THAT provider's console to check the key,
# quota, or status. Keyed by reviewer name; values are (button label, console URL).
_PROVIDER_DASHBOARD: dict[str, tuple[str, str]] = {
    "claude": ("🔧 Check Claude (Anthropic)", "https://console.anthropic.com/settings/usage"),
    "gpt": ("🔧 Check GPT (OpenAI)", "https://platform.openai.com/usage"),
    "gemini": ("🔧 Check Gemini (AI Studio)", "https://aistudio.google.com/"),
}


def _provider_dashboard_buttons(unavailable_reviewers) -> list[dict]:
    """A 'Check <provider>' button per down reviewer that maps to a known console.

    Deduped and order-stable. An unknown reviewer name is skipped (no button,
    rather than a wrong link).
    """
    seen: set[str] = set()
    buttons: list[dict] = []
    for reviewer in unavailable_reviewers or ():
        key = str(reviewer).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        entry = _PROVIDER_DASHBOARD.get(key)
        if entry:
            label, url = entry
            buttons.append({"text": label, "onClick": {"openLink": {"url": url}}})
    return buttons


def build_escalation_card(
    *,
    project_name: str,
    pr_number: int,
    pr_url: str,
    pr_title: str,
    tier: str,
    reason_short: str,
    reviewer_summaries: Mapping[str, str],
    approve_url: str = "",
    approve_merge_url: str = "",
    investigate_url: str = "",
    block_url: str = "",
    spend_breakdown: Mapping[str, float] | None = None,
    trigger: str = "",
    unavailable_reviewers: tuple[str, ...] = (),
) -> dict:
    """Build a Google Chat cardsV2 payload for an escalation.

    The card always carries an "Open PR" button. When ``approve_url`` is set
    (the operator's one-tap web app — see chat-approve/Code.gs) it also carries
    a "✅ Approve" button (posts OPERATOR APPROVE); when ``approve_merge_url`` is
    set it adds a "🚀 Approve & Merge" button (approve, then merge). When
    ``investigate_url`` / ``block_url`` are set (signed approve-webapp links) the
    card also carries "🔄 Send back (1 more round)" and "✋ Block" buttons, so the
    operator can send a head-lock / gut-check escalation back for another round
    without opening the PR. All are single-tap, no typing. ``spend_breakdown``
    (optional), when given, adds a
    "24h spend" line showing per-model cost so a budget-driven escalation says
    exactly where the money went. ``trigger`` (optional), the EscalationTrigger
    value as a string, swaps the engine's technical ``reason_short`` for a
    plain-language lead (see ``plain_escalation_lead``) in the body + subtitle;
    omit it to keep the literal reason (back-compatible default).
    ``unavailable_reviewers`` (optional) adds a "Check <provider>" button linking
    straight to each down reviewer's API console (gemini→AI Studio, claude→
    Anthropic, gpt→OpenAI) — the action that matches "a reviewer couldn't be
    reached".
    """
    reviewer_lines = "  •  ".join(
        f"{name}: {summary}" for name, summary in reviewer_summaries.items()
    ) or "(no reviewers yet)"

    if approve_url or approve_merge_url or investigate_url or block_url:
        # Only point at the PR for the actions that AREN'T one-tap buttons here.
        extras = []
        if not investigate_url:
            extras.append("INVESTIGATE")
        if not block_url:
            extras.append("BLOCK")
        if extras:
            instructions = (
                "Tap a button below, or open the PR for <b>"
                + "</b> / <b>".join(extras) + "</b>."
            )
        else:
            instructions = "Tap a button below."
    else:
        instructions = (
            "Open the PR and reply with one of:<br>"
            "<b>OPERATOR APPROVE</b> · <b>OPERATOR BLOCK &lt;reason&gt;</b> · "
            "<b>OPERATOR INVESTIGATE &lt;note&gt;</b>"
        )

    spend_line = format_spend_breakdown(spend_breakdown)
    spend_html = f"<b>24h spend:</b> {spend_line}<br>" if spend_line else ""

    # Plain-language lead when the caller passes the trigger; otherwise fall back
    # to the literal engine reason (keeps existing callers/tests unchanged).
    # Use the plain-language lead only for a RECOGNIZED trigger; an unknown trigger
    # keeps the literal reason so an unexpected state never hides its diagnostics.
    why_html = (
        f"{_esc(plain_escalation_lead(trigger))}<br>"
        if trigger in _PLAIN_LEAD
        else f"<b>Why:</b> {_esc(reason_short)}<br>"
    )
    body = (
        f"<b>{_esc(pr_title)}</b><br>"
        f"{why_html}"
        f"<b>Reviewers:</b> {_esc(reviewer_lines)}<br>"
        f"{spend_html}<br>"
        f"{instructions}"
    )

    buttons = []
    if approve_url:
        buttons.append(
            {"text": "✅ Approve", "onClick": {"openLink": {"url": approve_url}}}
        )
    if approve_merge_url:
        buttons.append(
            {"text": "🚀 Approve & Merge", "onClick": {"openLink": {"url": approve_merge_url}}}
        )
    if investigate_url:
        buttons.append(
            {"text": "🔄 Send back (1 more round)",
             "onClick": {"openLink": {"url": investigate_url}}}
        )
    if block_url:
        buttons.append(
            {"text": "✋ Block", "onClick": {"openLink": {"url": block_url}}}
        )
    # If a reviewer's own API is the thing that's down, jump straight to its
    # provider console (check key/quota/status) — placed before "Open PR" so the
    # action that matches the message is the first thing under the buttons.
    buttons.extend(_provider_dashboard_buttons(unavailable_reviewers))
    buttons.append(
        {"text": f"Open PR #{pr_number}", "onClick": {"openLink": {"url": pr_url}}}
    )

    return {
        "cardsV2": [
            {
                "cardId": f"escalation-pr-{pr_number}",
                "card": {
                    "header": {
                        "title": f"{project_name}: PR #{pr_number} needs you",
                        "subtitle": f"tier: {tier}" if trigger in _PLAIN_LEAD else f"tier: {tier} · {reason_short}",
                    },
                    "sections": [
                        {
                            "widgets": [
                                {"textParagraph": {"text": body}},
                                {"buttonList": {"buttons": buttons}},
                            ]
                        }
                    ],
                },
            }
        ]
    }


def build_ready_card(
    *,
    project_name: str,
    pr_number: int,
    pr_url: str,
    pr_title: str,
    tier: str,
    approve_merge_url: str = "",
) -> dict:
    """Build a Google Chat cardsV2 "ready to merge" notification.

    Posted on the dispatcher:ready-for-merge transition for ALL tiers (not just
    high-stakes escalations), so the operator's phone is pinged whenever a PR
    converges and is only waiting on their merge click. Carries a "🚀 Approve &
    Merge" button when configured, plus an "Open PR" button. The dispatcher
    never merges — the operator does.
    """
    buttons = []
    if approve_merge_url:
        buttons.append(
            {"text": "🚀 Approve & Merge", "onClick": {"openLink": {"url": approve_merge_url}}}
        )
    buttons.append(
        {"text": f"Open PR #{pr_number}", "onClick": {"openLink": {"url": pr_url}}}
    )

    body = (
        f"<b>{_esc(pr_title)}</b><br>"
        "All required reviewers approved and CI is green. "
        "<b>Ready for your merge.</b> The dispatcher never merges."
    )

    return {
        "cardsV2": [
            {
                "cardId": f"ready-pr-{pr_number}",
                "card": {
                    "header": {
                        "title": f"{project_name}: PR #{pr_number} ready to merge",
                        "subtitle": f"tier: {tier} · all reviewers approved",
                    },
                    "sections": [
                        {
                            "widgets": [
                                {"textParagraph": {"text": body}},
                                {"buttonList": {"buttons": buttons}},
                            ]
                        }
                    ],
                },
            }
        ]
    }


def build_budget_warning_card(
    *,
    project_name: str,
    spent_usd: float,
    ceiling_usd: float,
    breakdown: Mapping[str, float] | None = None,
) -> dict:
    """A one-time 'daily budget almost spent' heads-up.

    Sent the moment 24h spend crosses the warn threshold (default 80%), so the
    operator can throttle round counts or close PRs BEFORE reviews pause at the
    ceiling. Informational — no action buttons. ``breakdown`` (optional) adds a
    per-model "where it's going" line so the operator sees the dominant cost.
    """
    pct = int(round((spent_usd / ceiling_usd) * 100)) if ceiling_usd > 0 else 0
    spend_line = format_spend_breakdown(breakdown)
    spend_html = f"<b>Where it's going:</b> {spend_line}<br><br>" if spend_line else ""
    body = (
        f"<b>{_esc(project_name)}</b> has used <b>${spent_usd:.2f}</b> of its "
        f"${ceiling_usd:.2f} daily AI-review budget (~{pct}%).<br><br>"
        f"{spend_html}"
        f"Reviews <b>pause</b> at the ceiling. To avoid hitting it: throttle "
        f"round counts, close low-priority PRs, or raise the daily ceiling."
    )
    return {
        "cardsV2": [
            {
                "cardId": "budget-warning",
                "card": {
                    "header": {
                        "title": f"{project_name}: ~{pct}% of daily AI budget",
                        "subtitle": f"${spent_usd:.2f} / ${ceiling_usd:.2f}",
                    },
                    "sections": [{"widgets": [{"textParagraph": {"text": body}}]}],
                },
            }
        ]
    }


def build_increase_limit_url(repo: str, *, config_path: str = ".peer-review.json") -> str:
    """First-cut 'Increase limit' link: GitHub's web editor for the per-repo config.

    ``repo`` must be a well-formed 'owner/name' (both parts present, else "" — so a
    missing owner or name never yields an "owner/" or "/name" link). Opens
    ``.peer-review.json`` in the editor on the default branch (``HEAD``) so the
    operator can raise ``daily_cost_ceiling_usd``; the edit route resolves any
    default branch and works even if the file doesn't exist yet. (The one-tap
    pick-the-amount + auto-resume version is a follow-up — see BACKLOG.md.)
    """
    owner, _, name = (repo or "").partition("/")
    if not owner or not name:
        return ""
    return f"https://github.com/{owner}/{name}/edit/HEAD/{config_path}"


def build_budget_escalation_card(
    *,
    project_name: str,
    pr_number: int,
    pr_url: str,
    spent_usd: float,
    ceiling_usd: float,
    breakdown: Mapping[str, float] | None = None,
    increase_url: str = "",
    pr_title: str = "",
) -> dict:
    """Escalation card for a 24h spend-ceiling stop (``DAILY_COST_SPIKE``).

    The bot paused on *money*, not on a review — there may be no reviews at all —
    so this card deliberately OMITS Approve / Approve&Merge (tapping them could
    'approve' an unreviewed PR). It shows where the 24h money went and offers an
    'Increase limit' action plus Open PR — buttons that match the message.
    """
    spend_line = format_spend_breakdown(breakdown)
    # The blank-line separator lives INSIDE spend_html, so it's omitted entirely
    # when there's no breakdown (no stray empty line in the card).
    spend_html = f"<b>24h spend:</b> {spend_line}<br><br>" if spend_line else ""
    title_html = f"<b>{_esc(pr_title)}</b><br>" if pr_title else ""
    body = (
        f"{title_html}"
        "<b>Reviews paused — this repo hit its daily spending limit.</b><br>"
        f"Used <b>${spent_usd:.2f}</b> of the <b>${ceiling_usd:.2f}</b> daily "
        "AI-review budget.<br>"
        f"{spend_html}"
        "Raise the limit to resume, or leave it to stay paused. Approve/merge "
        "is intentionally hidden — this is a budget stop, not a review (reviewers "
        "may not have run)."
    )
    buttons = []
    if increase_url:
        buttons.append(
            {"text": "💵 Increase limit", "onClick": {"openLink": {"url": increase_url}}}
        )
    buttons.append(
        {"text": f"Open PR #{pr_number}", "onClick": {"openLink": {"url": pr_url}}}
    )
    return {
        "cardsV2": [
            {
                "cardId": f"budget-escalation-pr-{pr_number}",
                "card": {
                    "header": {
                        "title": f"{project_name}: spending paused (daily limit reached)",
                        "subtitle": (
                            f"${spent_usd:.2f} / ${ceiling_usd:.2f} · PR #{pr_number} paused"
                        ),
                    },
                    "sections": [
                        {
                            "widgets": [
                                {"textParagraph": {"text": body}},
                                {"buttonList": {"buttons": buttons}},
                            ]
                        }
                    ],
                },
            }
        ]
    }


def build_disagreement_card(
    *,
    project_name: str,
    pr_number: int,
    pr_url: str,
    pr_title: str,
    tier: str,
    reason_short: str,
    reviewer_summaries: Mapping[str, str],
    approve_url: str = "",
    approve_merge_url: str = "",
    investigate_url: str = "",
    block_url: str = "",
) -> dict:
    """Card for a reviewers-ran-but-didn't-auto-converge escalation.

    Two shapes, chosen by whether any required reviewer actually wants changes:
    - CONTESTED (someone requested changes): the panel is split. Show WHO is split
      and offer the tie-breaker — Approve & mark ready (override), Send back, or
      Block. NO one-tap merge — never one-tap-merge a contested PR.
    - ALL APPROVED (nobody wants changes, but it didn't converge — CI still running,
      the head moved right after they approved, or it hit the round cap): say
      exactly that — NOT "reviewers split" — and DO offer Approve & Merge, since
      there's nothing to override. GitHub branch protection is the backstop if CI
      isn't green yet.

    ``approve_merge_url`` / ``investigate_url`` / ``block_url`` (signed approve-webapp
    links) become one-tap buttons when set; otherwise the card falls back to the
    typed-OPERATOR-command prose.
    """
    # Bucket reviewers by verdict. A reviewer that errored / only commented / has
    # no verdict must NOT be shown as "want changes" — on a human-override card,
    # mislabeling who's blocking drives the wrong decision. Sort each bucket so the
    # output never depends on the caller's mapping order.
    buckets: dict[str, list[str]] = {"approve": [], "block": [], "other": []}
    for name, verdict in reviewer_summaries.items():
        # Summaries arrive as "<verdict> (round N)" — match the leading verdict
        # TOKEN exactly. Substring matching would mis-bucket ("disapprove" contains
        # "approve"); full-string equality would miss the "(round N)" suffix.
        token = str(verdict).strip().lower().split(" ", 1)[0]
        if token == "approve":
            buckets["approve"].append(name)
        elif token in ("request_changes", "block"):
            buckets["block"].append(name)
        else:
            buckets["other"].append(name)
    contested = bool(buckets["block"])  # at least one reviewer wants changes

    parts = []
    if buckets["approve"]:
        parts.append(f"✅ approve: {_esc(', '.join(sorted(buckets['approve'])))}")
    if buckets["block"]:
        parts.append(f"✋ want changes: {_esc(', '.join(sorted(buckets['block'])))}")
    if buckets["other"]:
        parts.append(f"❔ no clear verdict: {_esc(', '.join(sorted(buckets['other'])))}")
    # The blank-line separator lives inside split_html, so there's no stray break.
    split_html = f"{'  ·  '.join(parts)}<br><br>" if parts else ""

    if contested:
        lead = f"<b>Reviewers couldn't agree</b> — {_esc(reason_short)}.<br>"
    else:
        # Everyone approved, yet it didn't auto-converge — CI is still running, a
        # new commit landed right after they approved, or it hit the round cap.
        # Do NOT call this "split"; it isn't.
        lead = (
            "<b>All reviewers approved</b>, but it couldn't finish on its own "
            f"({_esc(reason_short)}) — usually CI still running, or the code "
            "changed right after they approved. Check the PR, then merge.<br>"
        )

    # One-tap buttons replace the typed OPERATOR commands when the approve-webapp
    # is configured; otherwise fall back to the command prose.
    if approve_url or approve_merge_url or investigate_url or block_url:
        tail = "read the concerns first." if contested else "check it first."
        instructions = f"Tap a button below, or open the PR to {tail}"
    else:
        instructions = (
            "Your call: <b>Approve</b> to mark ready, or open the PR and reply "
            "<b>OPERATOR INVESTIGATE &lt;note&gt;</b> (send back) or <b>OPERATOR BLOCK</b>."
        )
    body = (
        f"<b>{_esc(pr_title)}</b><br>"
        f"{lead}"
        f"{split_html}"
        f"{instructions}"
    )

    buttons = []
    if approve_url:
        buttons.append(
            {"text": "✅ Approve & mark ready", "onClick": {"openLink": {"url": approve_url}}}
        )
    # One-tap merge ONLY when nobody dissented — never one-tap-merge a contested PR.
    if approve_merge_url and not contested:
        buttons.append(
            {"text": "🚀 Approve & Merge", "onClick": {"openLink": {"url": approve_merge_url}}}
        )
    if investigate_url:
        buttons.append(
            {"text": "🔄 Send back (1 more round)",
             "onClick": {"openLink": {"url": investigate_url}}}
        )
    if block_url:
        buttons.append(
            {"text": "✋ Block", "onClick": {"openLink": {"url": block_url}}}
        )
    buttons.append(
        {"text": f"Open PR #{pr_number}", "onClick": {"openLink": {"url": pr_url}}}
    )

    title_tail = "reviewers split" if contested else "approved — needs you"
    return {
        "cardsV2": [
            {
                "cardId": f"disagreement-pr-{pr_number}",
                "card": {
                    "header": {
                        "title": f"{_esc(project_name)}: PR #{pr_number} — {title_tail}",
                        "subtitle": f"tier: {_esc(tier)} · {_esc(reason_short)}",
                    },
                    "sections": [
                        {
                            "widgets": [
                                {"textParagraph": {"text": body}},
                                {"buttonList": {"buttons": buttons}},
                            ]
                        }
                    ],
                },
            }
        ]
    }


def sign_action(secret: str, *, repo: str, pr_number: int, action: str) -> str:
    """HMAC-SHA256 hex over the canonical ``repo:pr:action`` string.

    Binds an approve/merge link to one specific PR and action, so a leaked or
    hand-edited URL (e.g. pr=90 → pr=99) fails verification in the Apps Script.
    """
    msg = f"{repo}:{pr_number}:{action}".encode("utf-8")
    return hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def build_approve_url(
    base_url: str,
    *,
    repo: str,
    pr_number: int,
    action: str = "approve",
    signing_secret: str = "",
) -> str:
    """Build a signed one-tap link (approve or approve_merge) for a PR.

    ``base_url`` is the Apps Script /exec URL (it may already carry a ?token=…).
    Returns "" when no base is configured, so the card omits the button. When
    ``signing_secret`` is set, a ``sig`` HMAC bound to repo+pr+action is added;
    the Apps Script rejects any link whose signature doesn't match.
    """
    if not base_url:
        return ""
    params = {"repo": repo, "pr": pr_number, "action": action}
    if signing_secret:
        params["sig"] = sign_action(
            signing_secret, repo=repo, pr_number=pr_number, action=action
        )
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}{urllib.parse.urlencode(params)}"


def send_chat_message(webhook_url: str, payload: dict, *, timeout: int = 20) -> None:
    """POST a payload to a Google Chat incoming webhook.

    Raises on missing URL or HTTP/network error; the caller decides how to
    handle failure (main._send_escalation swallows it after the durable
    PR-comment path has run).
    """
    if not webhook_url:
        raise ValueError("GOOGLE_CHAT_WEBHOOK_URL is required")
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=UTF-8"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read()


__all__ = [
    "build_escalation_card",
    "build_ready_card",
    "build_budget_warning_card",
    "format_spend_breakdown",
    "build_approve_url",
    "sign_action",
    "send_chat_message",
    "plain_escalation_lead",
    "known_plain_lead",
]
