"""Resend-based email sender for escalation notifications.

Section 6 of the design doc defines the email format. This module sends
that email via Resend's API. On failure, the orchestrator falls back to
posting a tagged PR comment + opening a GitHub issue (handled in main.py).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional


RESEND_API_URL = "https://api.resend.com/emails"
DEFAULT_FROM = "AI Peer Review <onboarding@resend.dev>"


@dataclass(frozen=True)
class EmailMessage:
    to: str
    subject: str
    text: str
    from_address: str = DEFAULT_FROM


class ResendClient:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("RESEND_API_KEY is required")
        self._api_key = api_key

    def send(self, message: EmailMessage) -> Optional[str]:
        body = {
            "from": message.from_address,
            "to": [message.to],
            "subject": message.subject,
            "text": message.text,
        }
        req = urllib.request.Request(
            RESEND_API_URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                return payload.get("id")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise RuntimeError(
                f"Resend API HTTP {exc.code}: {detail[:500]}"
            ) from exc


def build_escalation_email(
    *,
    project_name: str,
    pr_number: int,
    pr_url: str,
    pr_title: str,
    tier: str,
    branch: str,
    reason_short: str,
    detail: str,
    reviewer_summaries: dict[str, str],
    ci_status: str,
    head_sha: str,
    diff_summary: str,
    workflow_run_url: str,
    is_budget_stop: bool = False,
) -> EmailMessage:
    """Build the escalation email per Section 6 of the design doc.

    For a budget stop (``is_budget_stop``) the "Your call" section omits OPERATOR
    APPROVE — reviews may not have run, so approving would mark an UNREVIEWED PR
    ready. The operator is pointed at raising the ceiling instead. This keeps the
    email + durable PR-comment consistent with the Chat budget card (no approve
    path on a money stop), across every channel.
    """

    subject = f"[{project_name}] PR #{pr_number} — {reason_short}"

    reviewer_lines = "\n".join(
        f"{name + ':':<8} {summary}"
        for name, summary in reviewer_summaries.items()
    ) or "(no reviewers responded yet)"

    if is_budget_stop:
        your_call = (
            "== Your call ==\n"
            "Reviews are PAUSED on the 24h spend ceiling — reviewers may not have "
            "run on this PR, so there is nothing reviewed to approve. To resume, "
            "raise daily_cost_ceiling_usd in .peer-review.json. Other options:\n"
            "  OPERATOR PAUSE   stop touching this PR until OPERATOR RESUME\n"
            "  OPERATOR KILL    close this PR (branch is NOT deleted)\n"
            "Approving is intentionally not offered here — it would mark an "
            "unreviewed PR ready to merge."
        )
    else:
        your_call = (
            "== Your call ==\n"
            "Reply on the PR with one of (matches Section 7 of the design doc):\n"
            "  OPERATOR APPROVE\n"
            "      add approving review; mark ready for operator merge;\n"
            "      dispatcher does NOT merge. You merge manually on GitHub.\n"
            "  OPERATOR BLOCK <reason>\n"
            "      add changes-requested review with reason; pause dispatcher loop\n"
            "  OPERATOR INVESTIGATE <note>\n"
            "      send PR back to reviewers for another round with note as context\n"
            "  OPERATOR DISCUSS <your text>\n"
            "      post your text as a PR comment, triggers a new review round\n"
            "  OPERATOR PAUSE\n"
            "      dispatcher stops touching this PR until OPERATOR RESUME\n"
            "  OPERATOR RESUME\n"
            "      re-enable dispatcher on a paused PR\n"
            "  OPERATOR KILL\n"
            "      close PR only. Branch is NOT deleted (no contents:write)."
        )

    text = f"""\
PR:      {pr_url}
Title:   {pr_title}
Tier:    {tier}
Branch:  {branch}

== Why this needs you ==
{detail or reason_short}

== Reviewer summary ==
{reviewer_lines}

{your_call}

== CI status ==
{ci_status} on commit {head_sha}

== Diff summary ==
{diff_summary}

— {project_name} · AI peer review dispatcher
   Run: {workflow_run_url}
"""

    return EmailMessage(to="", subject=subject, text=text)


__all__ = [
    "RESEND_API_URL",
    "DEFAULT_FROM",
    "EmailMessage",
    "ResendClient",
    "build_escalation_email",
]
