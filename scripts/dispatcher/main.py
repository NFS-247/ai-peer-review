"""Entry point for the AI peer review dispatcher.

Invoked by .github/workflows/ai-peer-review.yml on every PR event. Reads the
event from $GITHUB_EVENT_PATH, decides what to do, executes one round of
dispatcher logic, exits.

Event routing (Section 5, 11.5, 12.5 of the design doc):
- pull_request (opened/synchronize/reopened): run a review round.
- check_run (completed): re-evaluate convergence ONLY (no new review round).
  Used so a PR that was waiting on CI becomes ready once CI passes.
- issue_comment (created): operator-command handling ONLY. Never runs a
  review round. Comments authored by the dispatcher bot are ignored entirely
  to prevent self-trigger loops.

This module orchestrates the other modules. Business rules live in classify,
verdict, converge, parse_reply, and escalation.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .ai_client import AIClient
from .ai_prompt import build_review_prompt
from .call_claude import ClaudeClient
from .call_gemini import GeminiClient
from .call_gpt import GPTClient
from .classify import classify
from .config import DispatcherConfig, load_from_env
from .converge import CIStatus, check_convergence
from .call_google_chat import (
    build_approve_url,
    build_budget_warning_card,
    build_escalation_card,
    build_ready_card,
    send_chat_message,
)
from .email_send import EmailMessage, ResendClient, build_escalation_email
from .escalation import (
    COOLDOWN_GATED_TRIGGERS,
    EscalationTrigger,
    cooldown_elapsed,
    decide_escalation,
)
from .github_api import GitHubAPI, PRComment
from .parse_reply import (
    CMD_APPROVE,
    CMD_BLOCK,
    CMD_DISCUSS,
    CMD_INVESTIGATE,
    CMD_KILL,
    CMD_PAUSE,
    CMD_RESUME,
    Command,
    CommandError,
    format_command_error_reply,
    parse_operator_command,
)
from .post_review import post_ai_review
from .redact import redact, register_secret
from . import state as label_state
from . import global_state
from . import usage
from . import usage_ledger
from .verdict import (
    Verdict,
    compute_diff_sha256,
    parse_signed_verdict_from_comment,
)


# The login GitHub assigns to comments posted with the Actions GITHUB_TOKEN.
# All dispatcher-authored comments (verdicts, state, ready, escalation) are
# authored by this account. Only verdicts from this author are trusted.
DISPATCHER_BOT_LOGIN = "github-actions[bot]"

# The workflow's own check-run name. check_run events for our own workflow
# must be ignored to avoid re-triggering on our own completion.
OWN_WORKFLOW_CHECK_NAMES = frozenset({"dispatch", "AI Peer Review"})


def _read_event() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path or not Path(path).exists():
        raise RuntimeError("GITHUB_EVENT_PATH is not set or file missing")
    return json.loads(Path(path).read_text())


def _pr_number_from_event(event: dict, event_name: str) -> Optional[int]:
    if event_name == "pull_request":
        return event.get("pull_request", {}).get("number")
    if event_name == "issue_comment":
        if event.get("issue", {}).get("pull_request"):
            return event.get("issue", {}).get("number")
        return None
    if event_name == "check_run":
        prs = event.get("check_run", {}).get("pull_requests", []) or []
        if prs:
            return prs[0].get("number")
        return None
    return None


def _operator_user_id(api: GitHubAPI, login: str) -> int:
    data = api._request("GET", f"/users/{login}")  # noqa: SLF001
    return int(data.get("id", 0))


def _ci_status_for(api: GitHubAPI, head_sha: str) -> CIStatus:
    """CI status for a commit, EXCLUDING the dispatcher's own workflow runs.

    If the check-runs API cannot be read (e.g. a transient error or a missing
    permission), degrade to PENDING rather than crashing the whole dispatcher.
    PENDING is the safe degradation: convergence requires CI == SUCCESS, so an
    unreadable CI status can never mark a PR ready, and the next check_run or
    push event re-evaluates. The correct permission (checks: read) is declared
    in the workflow; this guard prevents a single API hiccup from taking the
    dispatcher down.
    """
    try:
        all_runs = api.list_check_runs(head_sha)
    except Exception as exc:  # noqa: BLE001
        print(f"_ci_status_for: check-runs read failed: {exc}", file=sys.stderr)
        return CIStatus.PENDING

    runs = [r for r in all_runs if r.name not in OWN_WORKFLOW_CHECK_NAMES]
    if not runs:
        return CIStatus.NONE
    if any(r.status != "completed" for r in runs):
        return CIStatus.PENDING
    if any(r.conclusion not in ("success", "neutral", "skipped") for r in runs):
        return CIStatus.FAILURE
    return CIStatus.SUCCESS


def _build_client(reviewer: str, cfg: DispatcherConfig) -> Optional[AIClient]:
    try:
        if reviewer == "claude" and cfg.anthropic_api_key:
            return ClaudeClient(api_key=cfg.anthropic_api_key, model=cfg.claude_model or None)
        if reviewer == "gpt" and cfg.openai_api_key:
            return GPTClient(api_key=cfg.openai_api_key, model=cfg.gpt_model or None)
        if reviewer == "gemini" and cfg.gemini_api_key:
            return GeminiClient(api_key=cfg.gemini_api_key, model=cfg.gemini_model or None)
    except Exception:
        return None
    return None


def _trusted_existing_verdicts(
    api: GitHubAPI, pr_number: int, verdict_secret: str
) -> list[Verdict]:
    """Parse verdicts that are BOTH dispatcher-bot-authored AND HMAC-signed.

    Per design Section 12.5 (hardened): a verdict counts only if its comment
    is authored by github-actions[bot] AND the verdict block carries a valid
    dispatcher signature. This closes the "another Actions workflow forges a
    verdict as github-actions[bot]" hole, because forging requires
    DISPATCHER_VERDICT_SECRET. If no secret is configured, nothing counts
    (fail-safe: a missing secret can never produce a false approval).
    """
    if not verdict_secret:
        return []
    out: list[Verdict] = []
    for c in api.list_pr_comments(pr_number):
        if c.author_login != DISPATCHER_BOT_LOGIN:
            continue
        v = parse_signed_verdict_from_comment(c.body, verdict_secret)
        if v is not None and v.pr_number == pr_number:
            out.append(v)
    return out


# Return values for _handle_operator_command.
CMD_RESULT_NOT_COMMAND = "not_command"
CMD_RESULT_HANDLED = "handled"
CMD_RESULT_RUN_ROUND = "run_round"  # operator wants a fresh review round


def _handle_operator_command(
    *,
    cfg: DispatcherConfig,
    api: GitHubAPI,
    pr_number: int,
    comment_body: str,
    author_user_id: int,
    operator_user_id: int,
) -> tuple[str, str]:
    """Handle an operator command. Returns ``(status, operator_note)``.

    ``status`` is one of the CMD_RESULT_* constants. ``operator_note`` is the
    operator's free text for INVESTIGATE/DISCUSS, threaded into the triggered
    review round so reviewers see the instruction (design Section 7). It is
    empty for all other commands.

    INVESTIGATE and DISCUSS return CMD_RESULT_RUN_ROUND so the caller runs a
    fresh review round. This is safe from self-loops: the round runs in the
    same invocation (not by emitting an event), and dispatcher-authored
    comments are ignored by the issue_comment handler.
    """
    parsed = parse_operator_command(
        comment_body,
        author_user_id=author_user_id,
        operator_user_id=operator_user_id,
    )
    if parsed is None:
        return CMD_RESULT_NOT_COMMAND, ""
    if isinstance(parsed, CommandError):
        api.post_comment(pr_number, format_command_error_reply(parsed))
        return CMD_RESULT_HANDLED, ""

    cmd: Command = parsed
    labels = api.list_labels(pr_number)

    if cmd.verb == CMD_APPROVE:
        api.submit_review(
            pr_number,
            event="APPROVE",
            body="Operator approved. Dispatcher does NOT merge — see "
                 "design doc Section 10. Operator performs the merge "
                 "click manually.",
        )
        label_state.set_ready(api, pr_number, labels)
        api.post_comment(pr_number, "✅ Ready for operator merge. Dispatcher will not merge; you click the merge button.")
        return CMD_RESULT_HANDLED, ""

    if cmd.verb == CMD_BLOCK:
        api.submit_review(
            pr_number,
            event="REQUEST_CHANGES",
            body=f"Operator blocked: {cmd.args}",
        )
        label_state.set_paused(api, pr_number, labels)
        return CMD_RESULT_HANDLED, ""

    if cmd.verb == CMD_INVESTIGATE:
        api.post_comment(
            pr_number,
            f"Operator requested another review round with note:\n\n> {cmd.args}",
        )
        # Clearing pause so the round can run if the PR was previously paused
        # by a BLOCK; INVESTIGATE is an explicit "look again" instruction.
        label_state.clear_paused(api, pr_number, api.list_labels(pr_number))
        return CMD_RESULT_RUN_ROUND, cmd.args

    if cmd.verb == CMD_DISCUSS:
        api.post_comment(pr_number, f"_Operator says:_ {cmd.args}")
        return CMD_RESULT_RUN_ROUND, cmd.args

    if cmd.verb == CMD_PAUSE:
        label_state.set_paused(api, pr_number, labels)
        api.post_comment(pr_number, "⏸ Dispatcher paused on this PR. Use `OPERATOR RESUME` to re-enable.")
        return CMD_RESULT_HANDLED, ""

    if cmd.verb == CMD_RESUME:
        label_state.clear_paused(api, pr_number, labels)
        api.post_comment(pr_number, "▶ Dispatcher resumed on this PR.")
        return CMD_RESULT_HANDLED, ""

    if cmd.verb == CMD_KILL:
        api.close_pr(pr_number)
        api.post_comment(
            pr_number,
            "🛑 PR closed by operator command. Branch was NOT deleted — "
            "the dispatcher has no `contents: write` permission. Delete "
            "the branch manually on GitHub if you want it gone.",
        )
        return CMD_RESULT_HANDLED, ""

    return CMD_RESULT_HANDLED, ""


def _send_escalation(
    *,
    cfg: DispatcherConfig,
    api: GitHubAPI,
    pr_number: int,
    pr_url: str,
    pr_title: str,
    tier: str,
    branch: str,
    head_sha: str,
    reason_short: str,
    detail: str,
    reviewer_summaries: dict[str, str],
    ci_status: CIStatus,
    diff_summary: str,
    workflow_run_url: str,
    spend_breakdown: Optional[dict] = None,
) -> None:
    """Send the escalation email, with a guaranteed PR-comment fallback.

    Per design Section 6 and 9: email failure must fall back to a PR comment.
    The dispatcher never silently fails to notify. ``spend_breakdown`` (optional)
    is the 24h per-model spend, surfaced on the Chat card for budget escalations.
    """
    body_text = build_escalation_email(
        project_name=cfg.project_name,
        pr_number=pr_number,
        pr_url=pr_url,
        pr_title=pr_title,
        tier=tier,
        branch=branch,
        reason_short=reason_short,
        detail=detail,
        reviewer_summaries=reviewer_summaries,
        ci_status=ci_status.value,
        head_sha=head_sha,
        diff_summary=diff_summary,
        workflow_run_url=workflow_run_url,
    ).text

    # Push channel: ping the operator's phone via Google Chat if configured.
    # Best-effort and additional — the email/PR-comment path below is the
    # guaranteed durable record, so a Chat failure never loses the escalation.
    if cfg.google_chat_webhook_url:
        try:
            card = build_escalation_card(
                project_name=cfg.project_name,
                pr_number=pr_number,
                pr_url=pr_url,
                pr_title=pr_title,
                tier=tier,
                reason_short=reason_short,
                reviewer_summaries=reviewer_summaries,
                approve_url=build_approve_url(
                    cfg.approve_webapp_url or "",
                    repo=cfg.repo_name,
                    pr_number=pr_number,
                    action="approve",
                    signing_secret=cfg.approve_signing_secret or "",
                ),
                approve_merge_url=build_approve_url(
                    cfg.approve_webapp_url or "",
                    repo=cfg.repo_name,
                    pr_number=pr_number,
                    action="approve_merge",
                    signing_secret=cfg.approve_signing_secret or "",
                ),
                spend_breakdown=spend_breakdown,
            )
            send_chat_message(cfg.google_chat_webhook_url, card)
        except Exception as exc:  # noqa: BLE001
            # Never raise: the durable channels still run below.
            print(
                f"Google Chat notification failed ({type(exc).__name__}); "
                f"falling through to email/PR-comment.",
                file=sys.stderr,
            )

    emailed = False
    if cfg.resend_api_key and cfg.operator_email:
        msg = EmailMessage(
            to=cfg.operator_email,
            subject=f"[{cfg.project_name}] PR #{pr_number} — {reason_short}",
            text=body_text,
        )
        try:
            ResendClient(cfg.resend_api_key).send(msg)
            emailed = True
        except Exception as exc:  # noqa: BLE001
            api.post_comment(
                pr_number,
                f"⚠ Escalation email failed to send "
                f"(`{type(exc).__name__}`). Posting the escalation here as "
                f"fallback so it is never silently lost.\n\n"
                f"@{cfg.operator_github_login}\n\n"
                f"```\n{body_text}\n```",
            )
            return

    if not emailed:
        # No email configured at all: PR-comment fallback so the operator is
        # still notified.
        api.post_comment(
            pr_number,
            f"📣 Escalation (no email configured). "
            f"@{cfg.operator_github_login}\n\n```\n{body_text}\n```",
        )


def _now_ts() -> float:
    """Wall-clock UTC timestamp. Module-level so tests can monkeypatch it."""
    return datetime.now(timezone.utc).timestamp()


def _record_pending_escalation(
    cross: "label_state.CrossRunState",
    head_sha: str,
    *,
    trigger: str,
    reason_short: str,
    detail: str,
) -> None:
    """Record/refresh a deferred (cooldown-gated) escalation on ``cross``.

    The quiet timer (re)starts only when this is a NEW stall — a different head
    than the one already pending, or none pending yet. Re-evaluating the same
    head (e.g. an INVESTIGATE re-run) keeps the original timer so the operator
    isn't kept waiting forever by repeated same-head events.
    """
    if cross.pending_escalation_head_sha != head_sha or not cross.pending_escalation_since:
        cross.pending_escalation_since = _now_ts()
    cross.pending_escalation_head_sha = head_sha
    cross.pending_escalation_trigger = trigger
    cross.pending_escalation_reason_short = reason_short
    cross.pending_escalation_detail = detail


def _supersede_prior_escalation(
    api: GitHubAPI, pr_number: int, cross: "label_state.CrossRunState"
) -> None:
    """A new commit landed after a cooldown escalation already pinged: clear the
    stale escalated state and re-arm, so a fresh stall produces exactly one new
    ping. Incoming webhooks can't edit the old card, so we post a short note."""
    labels = api.list_labels(pr_number)
    if label_state.LABEL_ESCALATED in labels:
        api.remove_label(pr_number, label_state.LABEL_ESCALATED)
    cross.escalated_head_sha = ""
    cross.clear_pending_escalation()
    api.post_comment(
        pr_number,
        "🔄 New commit after the last escalation — re-reviewing. The previous "
        "escalation is superseded; you'll get one fresh ping only if this "
        "stalls again.",
    )


def _mark_ready_and_notify(
    *,
    cfg: DispatcherConfig,
    api: GitHubAPI,
    pr_number: int,
    pr_url: str,
    pr_title: str,
    tier: str,
    body: str,
) -> None:
    """Mark a PR ready for operator merge and notify — once.

    Posts the durable PR comment AND a mobile "ready to merge" Chat ping for
    ALL tiers (not just high-stakes escalations), but only on the transition to
    ready, so a re-evaluation never double-pings. The Chat ping is best-effort;
    its failure never blocks marking the PR ready.
    """
    labels = api.list_labels(pr_number)
    if label_state.LABEL_READY in labels:
        return  # already ready: do not duplicate the comment or the ping
    label_state.set_ready(api, pr_number, labels)
    api.post_comment(pr_number, body)
    if not cfg.google_chat_webhook_url:
        return
    try:
        card = build_ready_card(
            project_name=cfg.project_name,
            pr_number=pr_number,
            pr_url=pr_url,
            pr_title=pr_title,
            tier=tier,
            approve_merge_url=build_approve_url(
                cfg.approve_webapp_url or "",
                repo=cfg.repo_name,
                pr_number=pr_number,
                action="approve_merge",
                signing_secret=cfg.approve_signing_secret or "",
            ),
        )
        send_chat_message(cfg.google_chat_webhook_url, card)
    except Exception as exc:  # noqa: BLE001
        print(
            f"ready-for-merge Chat ping failed ({type(exc).__name__}); "
            f"PR is still marked ready via label + comment.",
            file=sys.stderr,
        )


def _maybe_fire_due_escalation(*, cfg: DispatcherConfig, api: GitHubAPI, pr_number: int) -> bool:
    """Fire a deferred escalation if its cooldown has elapsed on the same head.

    Called on every event that could be the moment a quiet stall becomes ripe:
    CI completion, a scheduled sweep, or the tail of a review round. Returns
    True if it pinged. No-ops (and leaves pending intact) when not yet due, and
    self-heals when the stall has resolved (ready/paused/escalated already, or a
    new commit superseded the pending head).
    """
    secret = cfg.verdict_secret or ""
    if not secret:
        return False
    ctx = _resolve_pr_context(api, pr_number)
    if ctx["state"] != "open":
        return False
    labels = api.list_labels(pr_number)
    if (
        label_state.is_paused(labels)
        or label_state.is_escalated(labels)
        or label_state.LABEL_READY in labels
    ):
        return False
    cross = label_state.read_cross_run_state(api, pr_number, DISPATCHER_BOT_LOGIN, secret)
    if not cooldown_elapsed(
        pending_since=cross.pending_escalation_since,
        pending_head_sha=cross.pending_escalation_head_sha,
        current_head_sha=ctx["head_sha"],
        now_ts=_now_ts(),
        cooldown_minutes=cfg.escalation_cooldown_minutes,
    ):
        return False

    tier = label_state.read_tier_label(labels) or "high_stakes"
    verdicts = _trusted_existing_verdicts(api, pr_number, secret)
    reviewer_summaries = {v.reviewer: f"{v.verdict} (round {v.round})" for v in verdicts}
    reason_short = cross.pending_escalation_reason_short or "reviewers requested changes"
    detail = cross.pending_escalation_detail

    label_state.set_escalated(api, pr_number, labels)
    cross.escalated_head_sha = ctx["head_sha"]
    cross.clear_pending_escalation()
    label_state.write_cross_run_state(api, pr_number, cross, secret)

    _send_escalation(
        cfg=cfg,
        api=api,
        pr_number=pr_number,
        pr_url=ctx["url"],
        pr_title=ctx["title"],
        tier=tier,
        branch=ctx["branch"],
        head_sha=ctx["head_sha"],
        reason_short=reason_short,
        detail=detail,
        reviewer_summaries=reviewer_summaries,
        ci_status=_ci_status_for(api, ctx["head_sha"]),
        diff_summary="",
        workflow_run_url=os.environ.get("GITHUB_RUN_URL", ""),
    )
    return True


def _run_cooldown_sweep(*, cfg: DispatcherConfig, api: GitHubAPI) -> int:
    """schedule event: fire any deferred escalations whose cooldown has elapsed.

    The dispatcher is event-driven; without this periodic sweep a PR that goes
    quiet after reviewers requested changes would never reach the operator's
    phone (no further event arrives to wake it). Best-effort per PR.
    """
    if not cfg.verdict_secret:
        return 0
    try:
        pr_numbers = api.list_open_pull_numbers()
    except Exception:  # noqa: BLE001
        return 0
    for n in pr_numbers:
        try:
            labels = api.list_labels(n)
            if not any(lbl.startswith(label_state.TIER_LABEL_PREFIX) for lbl in labels):
                continue
            _maybe_fire_due_escalation(cfg=cfg, api=api, pr_number=n)
        except Exception:  # noqa: BLE001
            continue
    return 0


def _resolve_pr_context(api: GitHubAPI, pr_number: int) -> dict:
    pr = api.get_pr(pr_number)
    return {
        "head_sha": pr.get("head", {}).get("sha", ""),
        "branch": pr.get("head", {}).get("ref", ""),
        "title": pr.get("title", ""),
        "body": pr.get("body", "") or "",
        "url": pr.get("html_url", ""),
        "state": pr.get("state", ""),
    }


def _secret_missing_guard(cfg: DispatcherConfig, api: GitHubAPI, pr_number: int) -> bool:
    """Fail closed if DISPATCHER_VERDICT_SECRET is not configured.

    Without the secret the dispatcher cannot verify any verdict, so it must
    not count verdicts, mark a PR ready, or run reviewers whose output it
    cannot trust. Returns True (and posts a one-time notice) when the secret
    is missing, so callers should abort. This closes the same-run leak: even
    freshly-computed verdicts are worthless without a verifiable secret.
    """
    if cfg.verdict_secret:
        return False
    labels = api.list_labels(pr_number)
    if "dispatcher:secret-missing" not in labels:
        api.add_labels(pr_number, ["dispatcher:secret-missing"])
        api.post_comment(
            pr_number,
            f"⛔ Dispatcher cannot operate: `DISPATCHER_VERDICT_SECRET` is not "
            f"configured. No reviews are run and nothing is marked ready "
            f"(fail-closed). @{cfg.operator_github_login} please set the "
            f"secret in repo settings, then push or comment to re-trigger.",
        )
    return True


def _run_convergence_only(
    *,
    cfg: DispatcherConfig,
    api: GitHubAPI,
    pr_number: int,
) -> int:
    """Re-evaluate convergence without running a new review round.

    Used on check_run completion: CI may have just passed, which can flip a
    PR from not-ready to ready without any new AI review.
    """
    ctx = _resolve_pr_context(api, pr_number)
    if ctx["state"] != "open":
        return 0

    if _secret_missing_guard(cfg, api, pr_number):
        return 0

    labels = api.list_labels(pr_number)
    if label_state.is_paused(labels):
        return 0

    tier = label_state.read_tier_label(labels)
    if tier is None:
        # Never classified yet; nothing to converge.
        return 0

    tier_cfg = cfg.tiers[tier]
    secret = cfg.verdict_secret or ""
    diff_text = api.get_pr_diff(pr_number)
    ci_status = _ci_status_for(api, ctx["head_sha"])

    cross = label_state.read_cross_run_state(api, pr_number, DISPATCHER_BOT_LOGIN, secret)

    # A new commit after a prior cooldown escalation supersedes it.
    if cross.escalated_head_sha and cross.escalated_head_sha != ctx["head_sha"]:
        _supersede_prior_escalation(api, pr_number, cross)
        label_state.write_cross_run_state(api, pr_number, cross, secret)
        labels = api.list_labels(pr_number)

    # Persistent-CI-failure handling on a CI-complete event: count the failed
    # attempt and, once it has failed across two rounds, escalate — but defer
    # the ping through the cooldown (CI failing usually means the dev is still
    # pushing fixes; don't ping mid-iteration).
    if ci_status == CIStatus.FAILURE:
        cross.ci_fix_attempts += 1
        if cross.ci_fix_attempts >= 2 and not label_state.is_escalated(labels):
            if cfg.escalation_cooldown_minutes > 0:
                _record_pending_escalation(
                    cross,
                    ctx["head_sha"],
                    trigger=EscalationTrigger.CI_PERSISTENT_FAILURE.value,
                    reason_short="CI is persistently failing",
                    detail="CI has failed across two rounds without being fixed. "
                           "The change needs a fix pushed before it can merge.",
                )
                label_state.write_cross_run_state(api, pr_number, cross, secret)
                _maybe_fire_due_escalation(cfg=cfg, api=api, pr_number=pr_number)
            else:
                label_state.write_cross_run_state(api, pr_number, cross, secret)
                label_state.set_escalated(api, pr_number, api.list_labels(pr_number))
                _send_escalation(
                    cfg=cfg,
                    api=api,
                    pr_number=pr_number,
                    pr_url=ctx["url"],
                    pr_title=ctx["title"],
                    tier=tier,
                    branch=ctx["branch"],
                    head_sha=ctx["head_sha"],
                    reason_short="CI is persistently failing",
                    detail="CI has failed across two rounds without being fixed. "
                           "The change needs a fix pushed before it can merge.",
                    reviewer_summaries={},
                    ci_status=ci_status,
                    diff_summary="",
                    workflow_run_url=os.environ.get("GITHUB_RUN_URL", ""),
                )
        else:
            label_state.write_cross_run_state(api, pr_number, cross, secret)
        return 0

    convergence = check_convergence(
        verdicts=_trusted_existing_verdicts(api, pr_number, secret),
        required_reviewers=tier_cfg.reviewers,
        current_head_sha=ctx["head_sha"],
        current_diff_sha256=compute_diff_sha256(diff_text),
        ci_status=ci_status,
        operator_pause_active=False,
    )

    if convergence.converged and not label_state.is_escalated(labels):
        changed_files = api.get_pr_files(pr_number)
        # High-stakes head-lock files still require operator approval even when
        # converged; do not mark ready in that case.
        from .escalation import _diff_touches_head_lock  # local import

        if tier == "high_stakes" and _diff_touches_head_lock(
            changed_files, cfg.repo_config.head_lock_paths
        ):
            # Operator sign-off required. Defer the ping through the cooldown
            # (the review round records the same pending stall); fire if due.
            if cfg.escalation_cooldown_minutes > 0 and not cross.has_pending_escalation():
                _record_pending_escalation(
                    cross,
                    ctx["head_sha"],
                    trigger=EscalationTrigger.HIGH_STAKES_AUTO.value,
                    reason_short="high-stakes file changed; operator review required",
                    detail="This PR touches files that require explicit operator "
                           "approval before merge, regardless of AI convergence.",
                )
                label_state.write_cross_run_state(api, pr_number, cross, secret)
            _maybe_fire_due_escalation(cfg=cfg, api=api, pr_number=pr_number)
            return 0
        # Converged and clear: ready for merge. Drop any pending stall, then
        # notify once (durable comment + mobile ping for all tiers).
        if cross.has_pending_escalation() or cross.escalated_head_sha:
            cross.clear_pending_escalation()
            cross.escalated_head_sha = ""
            label_state.write_cross_run_state(api, pr_number, cross, secret)
        _mark_ready_and_notify(
            cfg=cfg,
            api=api,
            pr_number=pr_number,
            pr_url=ctx["url"],
            pr_title=ctx["title"],
            tier=tier,
            body="✅ CI passed and all required reviewers approved this head "
                 "SHA. **Ready for operator merge.** Dispatcher does NOT merge.",
        )
        return 0

    # Not converged: a CI-complete event during a quiet stall is a chance to
    # fire a deferred escalation whose cooldown has elapsed.
    _maybe_fire_due_escalation(cfg=cfg, api=api, pr_number=pr_number)
    return 0


def _run_review_round(
    *,
    cfg: DispatcherConfig,
    api: GitHubAPI,
    pr_number: int,
    operator_note: str = "",
) -> int:
    ctx = _resolve_pr_context(api, pr_number)
    if ctx["state"] != "open":
        return 0

    # Fail closed: no secret means no trustable verdicts, so do not run
    # reviewers or count anything (closes the same-run no-secret leak).
    if _secret_missing_guard(cfg, api, pr_number):
        return 0

    head_sha = ctx["head_sha"]
    branch = ctx["branch"]
    pr_title = ctx["title"]
    pr_body = ctx["body"]
    pr_url = ctx["url"]

    changed_files = api.get_pr_files(pr_number)
    diff_text = api.get_pr_diff(pr_number)
    diff_sha = compute_diff_sha256(diff_text)

    classification = classify(changed_files, diff_text, cfg.repo_config)
    tier = classification.tier
    labels = api.list_labels(pr_number)
    label_state.set_tier(api, pr_number, tier, labels)
    labels = api.list_labels(pr_number)

    if label_state.is_paused(labels):
        print(f"PR #{pr_number} is paused; skipping", file=sys.stderr)
        return 0

    tier_cfg = cfg.tiers[tier]
    secret = cfg.verdict_secret or ""
    workflow_run_url = os.environ.get("GITHUB_RUN_URL", "")

    # 24h spend at the start of this round (before reviewers) — used both for
    # the preflight ceiling check and the budget pre-warning crossing check.
    daily_before = 0.0

    # ---- PREFLIGHT: 24h global spend ceiling, checked BEFORE any reviewer
    # call. If the repo is already over budget, this round must not burn even
    # one more AI call. Pause all in-flight PRs and escalate, then stop.
    if cfg.daily_cost_ceiling_usd > 0:
        try:
            daily_already, daily_breakdown = global_state.get_24h_breakdown(api)
        except Exception:  # noqa: BLE001
            daily_already, daily_breakdown = 0.0, {}
        daily_before = daily_already
        if daily_already >= cfg.daily_cost_ceiling_usd:
            label_state.set_escalated(api, pr_number, api.list_labels(pr_number))
            _pause_all_in_flight(
                api, reason="24h dispatcher spend ceiling already reached"
            )
            _send_escalation(
                cfg=cfg,
                api=api,
                pr_number=pr_number,
                pr_url=pr_url,
                pr_title=pr_title,
                tier=tier,
                branch=branch,
                head_sha=head_sha,
                reason_short="24-hour dispatcher spend ceiling reached",
                detail=f"Dispatcher spend across all PRs in the last 24h is "
                       f"${daily_already:.2f} (ceiling "
                       f"${cfg.daily_cost_ceiling_usd:.2f}). No reviewers were "
                       f"called this round. All in-flight reviews are paused "
                       f"pending operator review.",
                reviewer_summaries={},
                ci_status=_ci_status_for(api, head_sha),
                diff_summary=f"{len(changed_files)} file(s) changed",
                workflow_run_url=workflow_run_url,
                spend_breakdown=daily_breakdown,
            )
            return 0

    cross = label_state.read_cross_run_state(
        api, pr_number, DISPATCHER_BOT_LOGIN, secret
    )
    round_ = label_state.bump_round(api, pr_number, labels)

    # ---- run AI reviews for this round, tracking real cost and failures
    new_verdicts: list[Verdict] = []
    round_cost = 0.0
    usage_events: list[usage.UsageEvent] = []  # per-call metering for billing
    reviewer_failures: list[str] = []
    for reviewer in tier_cfg.reviewers:
        client = _build_client(reviewer, cfg)
        if client is None:
            reviewer_failures.append(reviewer)
            continue
        prompt = build_review_prompt(
            reviewer=reviewer,
            pr_number=pr_number,
            pr_title=pr_title,
            pr_body=pr_body,
            diff_text=diff_text,
            operator_note=operator_note,
            tier=tier,
            round_=round_,
            prior_review_history=[],
            project_description=cfg.repo_config.project_description,
            review_guidance=cfg.repo_config.review_guidance,
        )
        try:
            ai_resp = client.review(prompt)
            round_cost += ai_resp.cost_usd
            usage_events.append(usage.UsageEvent(
                reviewer=reviewer, model=ai_resp.model,
                input_tokens=ai_resp.input_tokens,
                output_tokens=ai_resp.output_tokens, cost_usd=ai_resp.cost_usd,
            ))
            try:
                verdict = post_ai_review(
                    api=api,
                    pr_number=pr_number,
                    reviewer=reviewer,
                    tier=tier,
                    round_=round_,
                    head_sha=head_sha,
                    diff_text=diff_text,
                    raw_ai_text=ai_resp.raw_text,
                    verdict_secret=secret,
                )
                new_verdicts.append(verdict)
            except ValueError:
                # Malformed AI JSON: retry once.
                ai_resp2 = client.review(prompt)
                round_cost += ai_resp2.cost_usd
                usage_events.append(usage.UsageEvent(
                    reviewer=reviewer, model=ai_resp2.model,
                    input_tokens=ai_resp2.input_tokens,
                    output_tokens=ai_resp2.output_tokens, cost_usd=ai_resp2.cost_usd,
                ))
                try:
                    verdict = post_ai_review(
                        api=api,
                        pr_number=pr_number,
                        reviewer=reviewer,
                        tier=tier,
                        round_=round_,
                        head_sha=head_sha,
                        diff_text=diff_text,
                        raw_ai_text=ai_resp2.raw_text,
                        verdict_secret=secret,
                    )
                    new_verdicts.append(verdict)
                except ValueError:
                    reviewer_failures.append(reviewer)
                    api.post_comment(
                        pr_number,
                        f"⚠ Dispatcher: reviewer `{reviewer}` returned a "
                        f"malformed response twice this round; not counted.",
                    )
        except Exception as exc:  # noqa: BLE001
            reviewer_failures.append(reviewer)
            api.post_comment(
                pr_number,
                f"⚠ Dispatcher: reviewer `{reviewer}` failed this round: "
                f"`{type(exc).__name__}: {exc}`.",
            )

    # Re-fetch CI status AFTER the reviewer calls. Those calls take 1-2
    # minutes, during which CI commonly flips from PENDING to SUCCESS. Using
    # the value captured at the top of this function caused the dispatcher to
    # decline auto-convergence even when all reviewers had approved and CI had
    # since gone green (observed on PR #81). Use this fresh value for the
    # fix-attempt accounting, convergence, and escalation alike.
    ci_status_now = _ci_status_for(api, head_sha)

    # ---- update cross-run state (numeric accounting)
    required_failed = [r for r in reviewer_failures if r in tier_cfg.reviewers]
    cross.cumulative_cost_usd += round_cost
    if required_failed:
        cross.consecutive_api_failures += 1
    else:
        cross.consecutive_api_failures = 0
    # Persistent-CI-failure lifecycle: each review round where CI is failing
    # increments the attempt counter; the trigger fires at >= 2.
    if ci_status_now == CIStatus.FAILURE:
        cross.ci_fix_attempts += 1
    else:
        cross.ci_fix_attempts = 0

    # If a new commit landed after a cooldown escalation already fired, that
    # escalation is stale — the dev addressed feedback. Clear it and re-arm so
    # a fresh stall produces exactly one new ping.
    if cross.escalated_head_sha and cross.escalated_head_sha != head_sha:
        _supersede_prior_escalation(api, pr_number, cross)

    # Per-reviewer cost for THIS round, attributed so the 24h ledger can show
    # which model is driving spend (surfaced on the warning/ceiling cards).
    round_by_provider: dict[str, float] = {}
    for ev in usage_events:
        round_by_provider[ev.reviewer] = round(
            round_by_provider.get(ev.reviewer, 0.0) + ev.cost_usd, 6
        )

    # ---- global 24h spend accounting (cross-PR ceiling)
    try:
        daily_total = global_state.record_and_get_24h_total(
            api, round_cost, by_provider=round_by_provider
        )
    except Exception:  # noqa: BLE001
        # If the ledger is unavailable, fall back to this PR's cumulative cost
        # so the daily trigger still has a non-zero signal rather than 0.
        daily_total = cross.cumulative_cost_usd

    # ---- budget pre-warning: ping ONCE on the round that crosses the warn
    # threshold (default 80%), so the operator can throttle before reviews pause
    # at the hard ceiling. Best-effort; never blocks the round.
    if cfg.google_chat_webhook_url and global_state.budget_warning_due(
        total_before=daily_before,
        total_after=daily_total,
        ceiling=cfg.daily_cost_ceiling_usd,
        warn_fraction=cfg.daily_cost_warn_fraction,
    ):
        try:
            _, warn_breakdown = global_state.get_24h_breakdown(api)
        except Exception:  # noqa: BLE001
            warn_breakdown = round_by_provider
        try:
            send_chat_message(
                cfg.google_chat_webhook_url,
                build_budget_warning_card(
                    project_name=cfg.project_name,
                    spent_usd=daily_total,
                    ceiling_usd=cfg.daily_cost_ceiling_usd,
                    breakdown=warn_breakdown,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"budget warning ping failed ({type(exc).__name__})", file=sys.stderr)

    # ---- per-tenant usage & billing ledger (foundation for invoicing).
    # Best-effort and additive: a billing-ledger hiccup must never break review.
    if usage_events:
        try:
            usage_ledger.record(
                api,
                usage_events,
                billing_mode=cfg.billing_mode,
                markup_multiplier=cfg.usage_markup_multiplier,
                dev_fee_usd=cfg.dev_fee_usd,
                secret=secret,
            )
        except Exception:  # noqa: BLE001
            pass

    # ---- convergence + escalation with REAL inputs
    all_verdicts = _trusted_existing_verdicts(api, pr_number, secret) + new_verdicts
    convergence = check_convergence(
        verdicts=all_verdicts,
        required_reviewers=tier_cfg.reviewers,
        current_head_sha=head_sha,
        current_diff_sha256=diff_sha,
        ci_status=ci_status_now,
        operator_pause_active=False,
    )

    # A required reviewer failing means an effective API outage for this PR;
    # the >=2 consecutive case drives the API-outage trigger, but a single
    # required-reviewer failure escalates immediately (no stuck state).
    api_outage_minutes = 60 if cross.consecutive_api_failures >= 2 else 0
    ci_failure_attempts = (
        cross.ci_fix_attempts if ci_status_now == CIStatus.FAILURE else 0
    )

    decision = decide_escalation(
        tier=tier,
        round_=round_,
        round_budget=tier_cfg.round_budget,
        max_review_rounds=cfg.max_review_rounds,
        convergence=convergence,
        changed_files=changed_files,
        per_pr_cost_usd=cross.cumulative_cost_usd,
        per_pr_cost_ceiling_usd=cfg.per_pr_cost_ceiling_usd,
        api_outage_minutes=api_outage_minutes,
        ci_failure_count_after_fix_attempts=ci_failure_attempts,
        daily_cost_usd=daily_total,
        daily_cost_ceiling_usd=cfg.daily_cost_ceiling_usd,
        required_reviewer_unavailable=bool(required_failed),
        head_lock_paths=cfg.repo_config.head_lock_paths,
    )

    reviewer_summaries = {
        v.reviewer: f"{v.verdict} (round {v.round})" for v in all_verdicts
    }
    for r in required_failed:
        reviewer_summaries.setdefault(r, "API failure this round")

    # ---- decide what to persist + whether/when to notify, then write state ONCE
    ready = convergence.converged and decision.trigger == EscalationTrigger.NONE
    deferred = False
    if ready:
        # The stall (if any) resolved; drop any pending/fired escalation state.
        cross.clear_pending_escalation()
        cross.escalated_head_sha = ""
    elif decision.trigger != EscalationTrigger.NONE:
        if (
            decision.trigger in COOLDOWN_GATED_TRIGGERS
            and cfg.escalation_cooldown_minutes > 0
        ):
            # Defer: record the pending stall; do NOT ping yet. The phone fires
            # only once the dev agent goes quiet (the scheduled sweep, or a
            # later event whose cooldown check passes) — never mid-iteration.
            _record_pending_escalation(
                cross,
                head_sha,
                trigger=decision.trigger.value,
                reason_short=decision.reason_short,
                detail=decision.detail,
            )
            deferred = True
        else:
            # Immediate (infra/budget) escalation supersedes any pending stall.
            cross.clear_pending_escalation()

    label_state.write_cross_run_state(api, pr_number, cross, secret)

    # ---- side effects
    if ready:
        _mark_ready_and_notify(
            cfg=cfg,
            api=api,
            pr_number=pr_number,
            pr_url=pr_url,
            pr_title=pr_title,
            tier=tier,
            body="✅ All required reviewers approved this head SHA. **Ready for "
                 "operator merge.** Dispatcher does NOT merge — operator clicks "
                 "the merge button manually.",
        )
        return 0

    if decision.trigger != EscalationTrigger.NONE:
        if deferred:
            # The cooldown may already have elapsed (e.g. an INVESTIGATE re-run
            # on a head that's been quiet); fire now if due, else stay silent.
            _maybe_fire_due_escalation(cfg=cfg, api=api, pr_number=pr_number)
            return 0
        label_state.set_escalated(api, pr_number, api.list_labels(pr_number))
        # The 24h global spend trigger is a project-wide safety stop: pause
        # ALL in-flight PRs (not just this one), matching the design's stated
        # "all in-flight reviews are paused" behavior. Attach the per-model 24h
        # breakdown so the operator sees what drove the spend.
        spend_breakdown = None
        if decision.trigger == EscalationTrigger.DAILY_COST_SPIKE:
            _pause_all_in_flight(api, reason="24h dispatcher spend ceiling reached")
            try:
                _, spend_breakdown = global_state.get_24h_breakdown(api)
            except Exception:  # noqa: BLE001
                spend_breakdown = round_by_provider
        _send_escalation(
            cfg=cfg,
            api=api,
            pr_number=pr_number,
            pr_url=pr_url,
            pr_title=pr_title,
            tier=tier,
            branch=branch,
            head_sha=head_sha,
            reason_short=decision.reason_short,
            detail=decision.detail,
            reviewer_summaries=reviewer_summaries,
            ci_status=ci_status_now,
            diff_summary=f"{len(changed_files)} file(s) changed",
            workflow_run_url=workflow_run_url,
            spend_breakdown=spend_breakdown,
        )

    return 0


def _pause_all_in_flight(api: GitHubAPI, *, reason: str) -> None:
    """Pause every open PR the dispatcher is tracking.

    Used when the 24-hour global spend ceiling is hit. Sets dispatcher:paused
    on each open PR that has a dispatcher tier label, so no further AI rounds
    run anywhere until the operator resumes. Best-effort: failures on
    individual PRs do not abort the sweep.
    """
    try:
        pr_numbers = api.list_open_pull_numbers()
    except Exception:  # noqa: BLE001
        return
    for n in pr_numbers:
        try:
            labels = api.list_labels(n)
            if not any(lbl.startswith(label_state.TIER_LABEL_PREFIX) for lbl in labels):
                continue
            if label_state.LABEL_PAUSED in labels:
                continue
            label_state.set_paused(api, n, labels)
            api.post_comment(
                n,
                f"⏸ Dispatcher paused on this PR: {reason}. All in-flight "
                f"reviews are paused. Use `OPERATOR RESUME` per PR to re-enable.",
            )
        except Exception:  # noqa: BLE001
            continue


def run() -> int:
    cfg = load_from_env()
    if not cfg.github_token:
        print("GITHUB_TOKEN not set; nothing to do", file=sys.stderr)
        return 0

    # Register every real secret value so redaction scrubs it from any comment
    # the dispatcher posts, even if it surfaces inside an exception string.
    for s in (
        cfg.anthropic_api_key, cfg.openai_api_key, cfg.gemini_api_key,
        cfg.resend_api_key, cfg.github_token, cfg.verdict_secret,
        cfg.google_chat_webhook_url, cfg.approve_webapp_url,
        cfg.approve_signing_secret,
    ):
        register_secret(s)

    api = GitHubAPI(cfg.github_token, cfg.repo_owner, cfg.repo_name)
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")

    # ---- schedule: periodic sweep that fires deferred escalations once the dev
    # agent has gone quiet past the cooldown. There is no PR in a schedule
    # event, so it is handled before the per-PR routing below.
    if event_name == "schedule":
        return _run_cooldown_sweep(cfg=cfg, api=api)

    event = _read_event()

    pr_number = _pr_number_from_event(event, event_name)
    if pr_number is None:
        print(f"No PR in event {event_name}; nothing to do", file=sys.stderr)
        return 0

    # ---- issue_comment: operator commands only; ignore the bot's own comments
    if event_name == "issue_comment":
        comment = event.get("comment", {}) or {}
        author = comment.get("user", {}) or {}
        author_login = author.get("login", "")

        # Self-loop guard: never react to the dispatcher's own comments.
        if author_login == DISPATCHER_BOT_LOGIN:
            return 0

        operator_id = _operator_user_id(api, cfg.operator_github_login)
        result, operator_note = _handle_operator_command(
            cfg=cfg,
            api=api,
            pr_number=pr_number,
            comment_body=comment.get("body", "") or "",
            author_user_id=int(author.get("id", 0)),
            operator_user_id=operator_id,
        )
        # INVESTIGATE / DISCUSS explicitly ask for a fresh review round. We run
        # it in-process here (not by emitting an event), so there is no
        # self-trigger: the dispatcher's own comments never re-enter this path
        # because bot-authored comments are filtered at the top of this branch.
        # The operator's note is threaded into the reviewers' prompt.
        if result == CMD_RESULT_RUN_ROUND:
            return _run_review_round(
                cfg=cfg, api=api, pr_number=pr_number, operator_note=operator_note
            )
        # All other operator commands are terminal for this event.
        return 0

    # ---- check_run completed: re-evaluate convergence only (no new round)
    if event_name == "check_run":
        check = event.get("check_run", {}) or {}
        if check.get("name", "") in OWN_WORKFLOW_CHECK_NAMES:
            # Ignore our own workflow's completion.
            return 0
        return _run_convergence_only(cfg=cfg, api=api, pr_number=pr_number)

    # ---- pull_request: run a review round
    if event_name == "pull_request":
        action = event.get("action", "")
        if action not in ("opened", "synchronize", "reopened"):
            return 0
        return _run_review_round(cfg=cfg, api=api, pr_number=pr_number)

    return 0


if __name__ == "__main__":
    sys.exit(run())
