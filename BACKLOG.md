# Backlog

Operator-tracked follow-ups, highest-impact first. Items tagged **[StockTrader]**
live in that repo — out of this session's scope (this session can only write to
`NFS-247/ai-peer-review`); they're recorded here so nothing is lost.

---

## P1 — Escalation EMAIL is broken (the alerting path) · [ai-peer-review]

**✅ SHIPPED — PR #2** (squash `c90fb708`, on `main` + `@v2`): configurable
verified sender (`email_from`, env > repo config > default), self-diagnosing
fallback with the real Resend error + hint, a hardened `_post_fallback_comment`
that never raises, and the first tests for the email send path. Panel-approved
3/3, operator-approved, merged, and live on `@v2`. **Operator still must verify a
Resend domain and set `email_from`** (in the consumer's `.peer-review.json`) for
email to actually deliver — until then escalations use the hardened PR-comment
fallback.

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

1. **✅ SHIPPED — Reviewer read-timeout too tight for large diffs.** PR #5
   (squash `ca39ba4`, on `main` + `@v2`): read timeouts (bare or URLError-wrapped)
   now route to a separate, bounded timeout-retry budget; the OpenAI read timeout
   is operator-tunable via `OPENAI_READ_TIMEOUT` (default 300s, clamped 600s), and
   gpt opts into one retry with a per-call `Idempotency-Key` so the retry can't
   double-bill (claude/gemini fail a timeout cleanly). Reviewed 3/3. This was the
   cause of most escalations this session.

2. **✅ DONE — `@v2` re-cut to `main`.** The `v2` branch was fast-forwarded to
   `main` (`c90fb70`) — an 8-commit delta (the timeout + email fixes plus docs/CI
   wiring), 287 tests green. Consumers (StockTrader/Canary) pick up both fixes on
   their next run. Rollback point if ever needed: old `v2` was `e0de49e`.

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

## P1 — Context-aware Chat escalation cards + per-user webhooks · [ai-peer-review]

**Operator-requested (this session).** Core principle: **the buttons on a Chat
card must match what the card is about.** Today every escalation posts the same
Approve / Approve&Merge card — wrong when the bot stopped on *money*, or is
*stuck*, not when it's waiting on an approval. Operator's words: "if it's
financial it should say increase or stay; if it's approving, approve & merge; if
it's 'what's going on', the buttons for that." And: you only get a ping when a PR
**converges**, so when reviewers **deadlock you get silence and sit there doing
nothing.**

Card variants (layout + buttons driven by the escalation trigger):
- **Budget / 24h ceiling hit (`DAILY_COST_SPIKE`)** — show the 24h spend
  breakdown (the proof) + **Increase limit (tap to pick the amount)** + **Stay /
  keep paused** + **Open PR**. Drop Approve/Approve&Merge. The increase is
  **one-tap**: posts a new `OPERATOR INCREASE <amount>` that sets a *persisted*
  ceiling override (bounded — a leaked link can't set it to ∞) **and auto-resumes**
  the dispatcher, so the operator never types anything into GitHub.
- **Ready to merge** (converged, or head-lock sign-off) — Approve & mark ready +
  Open PR. *(exists today.)*
- **Stuck / reviewers can't converge** — the missing ping. Say *what's* stuck
  (which reviewer is blocking and why — e.g. "Gemini blocking on X; Claude+GPT
  disagree, possible hallucination") with buttons to settle it: **Approve & mark
  ready** / **Send back with a note** / **Open PR**. Invariant: every PR ends in
  *merged* OR *an actionable ping* — never silent.
- **Reviewer red flag** — let a reviewer raise "human, look now" (a severe finding
  or sharp disagreement) that pings chat regardless of the mechanical triggers.
  *Open Q:* any single reviewer, or only on a split? (noise control.)

**Per-user webhook settings (front-door UI).** Each operator configures their
*own* webhook + type (Google Chat / Slack / Discord / generic) in the front door,
instead of one baked-in org secret — portable across users/tenants.

**Where it lives:** engine = per-trigger card builders in `call_google_chat.py`,
a new `OPERATOR INCREASE` (`parse_reply` + `main` + a persisted ceiling override
in `global_state`), and richer disagreement detail in the escalation. Front door
= the webhook-settings page + the chat-approve Apps Script handler for the new
button actions — lands once the front door (PR #1) is on `main`.

**Check first (may already work):** if stuck-escalations aren't reaching the
operator today, it's usually (a) no chat webhook on that repo (so it went to
email / a PR comment), or (b) the ping is deferred until the author stops pushing.
Verify before building.

---

## P2 — Auto-enroll candidates that pass the OOS gate · [StockTrader]

Currently a **manual POST**. Automate enrollment once a candidate passes the
out-of-sample gate, so promotion isn't gated on a human request.

---

## P3 — Sessions-file rotation + persistent state dir · [StockTrader]

Reviewer follow-ups from **#122**: rotate the sessions file and move state into a
**persistent state directory** (so it survives restarts and doesn't grow
unbounded).
