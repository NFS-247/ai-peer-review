# Backlog

Operator-tracked follow-ups, highest-impact first. Items tagged **[StockTrader]**
live in that repo — out of this session's scope (this session can only write to
`NFS-247/ai-peer-review`); they're recorded here so nothing is lost.

---

## P1 — Escalation EMAIL is broken (the alerting path) · [ai-peer-review]

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

## P2 — Auto-enroll candidates that pass the OOS gate · [StockTrader]

Currently a **manual POST**. Automate enrollment once a candidate passes the
out-of-sample gate, so promotion isn't gated on a human request.

---

## P3 — Sessions-file rotation + persistent state dir · [StockTrader]

Reviewer follow-ups from **#122**: rotate the sessions file and move state into a
**persistent state directory** (so it survives restarts and doesn't grow
unbounded).
