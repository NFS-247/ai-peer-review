"""Google Chat incoming-webhook sender for escalation notifications.

An optional push channel so the operator is pinged on their phone the moment a
PR needs a decision. Configured via the GOOGLE_CHAT_WEBHOOK_URL secret.

This is an ADDITIONAL channel layered on top of the email/PR-comment path in
main._send_escalation. The PR comment remains the guaranteed durable record, so
a Chat delivery failure can never lose a notification — it is best-effort.

Pure stdlib (urllib), no dependencies, consistent with the rest of the package.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping


def _esc(text: str) -> str:
    """Escape the small set of chars Google Chat treats as markup in text widgets."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


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
) -> dict:
    """Build a Google Chat cardsV2 payload for an escalation.

    The card always carries an "Open PR" button. When ``approve_url`` is set
    (the operator's one-tap approve web app — see chat-approve/Code.gs), it also
    carries a "✅ Approve" button that posts OPERATOR APPROVE for the operator,
    so a decision is a single tap with no typing.
    """
    reviewer_lines = "  •  ".join(
        f"{name}: {summary}" for name, summary in reviewer_summaries.items()
    ) or "(no reviewers yet)"

    if approve_url:
        instructions = (
            "Tap <b>✅ Approve</b> below, or open the PR for "
            "<b>BLOCK</b> / <b>INVESTIGATE</b>."
        )
    else:
        instructions = (
            "Open the PR and reply with one of:<br>"
            "<b>OPERATOR APPROVE</b> · <b>OPERATOR BLOCK &lt;reason&gt;</b> · "
            "<b>OPERATOR INVESTIGATE &lt;note&gt;</b>"
        )

    body = (
        f"<b>{_esc(pr_title)}</b><br>"
        f"<b>Why:</b> {_esc(reason_short)}<br>"
        f"<b>Reviewers:</b> {_esc(reviewer_lines)}<br><br>"
        f"{instructions}"
    )

    buttons = []
    if approve_url:
        buttons.append(
            {
                "text": "✅ Approve",
                "onClick": {"openLink": {"url": approve_url}},
            }
        )
    buttons.append(
        {
            "text": f"Open PR #{pr_number}",
            "onClick": {"openLink": {"url": pr_url}},
        }
    )

    return {
        "cardsV2": [
            {
                "cardId": f"escalation-pr-{pr_number}",
                "card": {
                    "header": {
                        "title": f"{project_name}: PR #{pr_number} needs you",
                        "subtitle": f"tier: {tier} · {reason_short}",
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


def build_approve_url(base_url: str, *, repo: str, pr_number: int) -> str:
    """Build the one-tap approve link for a PR from the configured web-app base.

    ``base_url`` is the Apps Script /exec URL (it may already carry a ?token=…).
    Returns "" when no base is configured, so the card simply omits the button.
    """
    if not base_url:
        return ""
    sep = "&" if "?" in base_url else "?"
    query = urllib.parse.urlencode({"repo": repo, "pr": pr_number, "action": "approve"})
    return f"{base_url}{sep}{query}"


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


__all__ = ["build_escalation_card", "build_approve_url", "send_chat_message"]
