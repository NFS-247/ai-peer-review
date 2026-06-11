# Backlog

Operator-tracked follow-ups, highest-impact first. Items tagged **[StockTrader]**
live in that repo — out of this session's scope (this session can only write to
`NFS-247/ai-peer-review`); they're recorded here so nothing is lost.

---

## P1 — Escalation EMAIL is broken (the alerting path) · [ai-peer-review]

**✅ FIXED — PR #2** (`claude/fix-escalation-email`): configurable verified
sender (`email_from`, env > repo config > default), self-diagnosing fallback
with the real Resend error + hint, a hardened `_post_fallback_comment` that never
raises, and the first tests for the email send path. Panel-approved 3/3 then
operator-approved; pending merge. Operator still must verify a Resend domain and
set `email_from` for email to actually deliver.

**Symptom (observed this session):** every escalation email failed with
`RuntimeError`. The operator only got alerts because the PR-comment fallback
fired — and **once even that didn't**, so an escalation was missed. This is the
alerting path; it must be reliable.

**Likely root cause:** `scripts/dispatcher/email_send.py` hard-codes
`DEFAULT_FROM = "AI Peer Review <onboarding@resend.dev>"`. `onboarding@resend.dev`
is Resend's **shared test sender**, which can only deliver to the Resend
account's *own* email. Sending to the operator's address returns **HTTP 403**
("you can only send testing emails to your own email address"), which
`ResendClient.send` turns into the `RuntimeError` seen every time.

**Fix:**
- Make the `from` address **configurable** (env, e.g. `EMAIL_FROM`) and require
  an address on a **verified Resend domain**. Document it in onboarding —
  without a verified sender, email cannot work.
- **Surface the Resend error body** in the fallback comment (it already includes
  the exception type; include the 403 detail) so the misconfig is self-diagnosing
  rather than a silent `RuntimeError`.
- **Harden the fallback** so an escalation is *never* lost: the "once it didn't
  post a fallback either" case means the fallback `post_comment` itself failed
  (likely a GitHub rate limit — see the retry/backoff added this session, which
  should help). Consider a last-resort path (open a tracking issue) if the PR
  comment also fails.
- **Test:** a 403 from Resend → `RuntimeError` → fallback comment posts.

---

## Engine findings from the self-review dogfood · [ai-peer-review]

Surfaced while the engine reviewed its own PRs (#1 front door, #2 email fix).
All are engine changes → ship via a deliberate `@v2` release (see next item).

1. **Reviewer read-timeout too tight for large diffs.** `ai_client.py` uses a
   120s read timeout for all reviewers. `gpt` repeatedly hit
   `TimeoutError` reviewing the large front-door PR (#1) while `claude`/`gemini`
   finished — so a big-but-valid PR can't get a clean 3/3. Make the timeout
   env-configurable with a higher default (~300s). PR #1 was operator-approved
   on 2/3 because of this (gpt failure was infra, not an objection).

2. **`@v2` is ~22 commits / ~2000 lines behind `main`.** Significant unreleased
   engine work (per-model spend pricing, GitHub rate-limit retry/backoff, model
   selection, escalation/ready fixes, budget pre-warning). Self-review **and all
   consumers** (StockTrader/Canary) run the older Cut-1 engine until a deliberate,
   tested `v2.x` release moves the `@v2` tag. The timeout fix above ships with it.

3. **Stale `dispatcher:secret-missing` label.** Added when
   `DISPATCHER_VERDICT_SECRET` is absent; **never removed** once the secret is
   provided (`main.py` only adds it). Cosmetic but misleading on the board/UI —
   PR #2 still carries it after reviews ran fine. Remove the label when the secret
   is present.

4. **Transient-failure escalations don't self-resolve.** A reviewer 503/timeout
   escalates the PR; even after all reviewers later approve, it stays escalated
   (ready-marking is gated on `not is_escalated`, `main.py:695`) until the operator
   acts. Both PRs hit this. When a PR escalated *solely* due to a transient
   provider error reaches full convergence, auto-clear the escalation and mark
   ready.

**Also shipped this session:** Front Door UI (Milestone 3) — PR #1, panel-approved
(claude+gemini; gpt timed out per #1 above).

---

## P2 — Auto-enroll candidates that pass the OOS gate · [StockTrader]

Currently a **manual POST**. Automate enrollment once a candidate passes the
out-of-sample gate, so promotion isn't gated on a human request.

---

## P3 — Sessions-file rotation + persistent state dir · [StockTrader]

Reviewer follow-ups from **#122**: rotate the sessions file and move state into a
**persistent state directory** (so it survives restarts and doesn't grow
unbounded).
