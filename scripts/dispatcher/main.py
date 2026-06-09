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
from .call_google_chat import build_approve_url, build_escalation_card, send_chat_message
from .email_send import EmailMessage, ResendClient, build_escalation_email
from .escalation import EscalationTrigger, decide_escalation
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
            return ClaudeClient(api_key=cfg.anthropic_api_key)
        if reviewer == "gpt" and cfg.openai_api_key:
            return GPTClient(api_key=cfg.openai_api_key)
        if reviewer == "gemini" and cfg.gemini_api_key:
            return GeminiClient(api_key=cfg.gemini_api_key)
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
) -> None:
    """Send the escalation email, with a guaranteed PR-comment fallback.

    Per design Section 6 and 9: email failure must fall back to a PR comment.
    The dispatcher never silently fails to notify.
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

    # Persistent-CI-failure handling on a CI-complete event: count the failed
    # attempt and escalate once it has failed across two rounds.
    if ci_status == CIStatus.FAILURE:
        cross = label_state.read_cross_run_state(
            api, pr_number, DISPATCHER_BOT_LOGIN, secret
        )
        cross.ci_fix_attempts += 1
        label_state.write_cross_run_state(api, pr_number, cross, secret)
        if cross.ci_fix_attempts >= 2 and not label_state.is_escalated(labels):
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

        if tier == "high_stakes" and _diff_touches_head_lock(changed_files):
            return 0
        if label_state.LABEL_READY not in labels:
            label_state.set_ready(api, pr_number, labels)
            api.post_comment(
                pr_number,
                "✅ CI passed and all required reviewers approved this head "
                "SHA. **Ready for operator merge.** Dispatcher does NOT merge.",
            )
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

    # ---- PREFLIGHT: 24h global spend ceiling, checked BEFORE any reviewer
    # call. If the repo is already over budget, this round must not burn even
    # one more AI call. Pause all in-flight PRs and escalate, then stop.
    if cfg.daily_cost_ceiling_usd > 0:
        try:
            daily_already = global_state.get_24h_total(api)
        except Exception:  # noqa: BLE001
            daily_already = 0.0
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
            )
            return 0

    cross = label_state.read_cross_run_state(
        api, pr_number, DISPATCHER_BOT_LOGIN, secret
    )
    round_ = label_state.bump_round(api, pr_number, labels)

    # ---- run AI reviews for this round, tracking real cost and failures
    new_verdicts: list[Verdict] = []
    round_cost = 0.0
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
        )
        try:
            ai_resp = client.review(prompt)
            round_cost += ai_resp.cost_usd
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

    # ---- update cross-run state
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
    label_state.write_cross_run_state(api, pr_number, cross, secret)

    # ---- global 24h spend accounting (cross-PR ceiling)
    try:
        daily_total = global_state.record_and_get_24h_total(api, round_cost)
    except Exception:  # noqa: BLE001
        # If the ledger is unavailable, fall back to this PR's cumulative cost
        # so the daily trigger still has a non-zero signal rather than 0.
        daily_total = cross.cumulative_cost_usd

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
    )

    reviewer_summaries = {
        v.reviewer: f"{v.verdict} (round {v.round})" for v in all_verdicts
    }
    for r in required_failed:
        reviewer_summaries.setdefault(r, "API failure this round")

    if convergence.converged and decision.trigger == EscalationTrigger.NONE:
        labels = api.list_labels(pr_number)
        label_state.set_ready(api, pr_number, labels)
        api.post_comment(
            pr_number,
            "✅ All required reviewers approved this head SHA. **Ready for "
            "operator merge.** Dispatcher does NOT merge — operator clicks "
            "the merge button manually.",
        )
        return 0

    if decision.trigger != EscalationTrigger.NONE:
        label_state.set_escalated(api, pr_number, api.list_labels(pr_number))
        # The 24h global spend trigger is a project-wide safety stop: pause
        # ALL in-flight PRs (not just this one), matching the design's stated
        # "all in-flight reviews are paused" behavior.
        if decision.trigger == EscalationTrigger.DAILY_COST_SPIKE:
            _pause_all_in_flight(api, reason="24h dispatcher spend ceiling reached")
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
