# Onboarding a project to AI Peer Review

This is the step-by-step for putting a project under the AI peer-review engine
(`NFS-247/ai-peer-review`). It's written so it can be **handed to someone else**
to run.

---

## Before you start — what this costs you and requires from you

Know these up front so there are **no surprises** later:

1. **It costs money.** Every review spends real AI tokens — usually pennies per
   PR, but it adds up across many PRs. You set spend ceilings
   (`per_pr_cost_ceiling_usd`, `daily_cost_ceiling_usd`); the system stops and
   asks you before it blows past them. Nothing runs for free.
2. **You supply and pay for the AI keys** — Anthropic, OpenAI, Gemini — *today*.
   *(In the future product version, the platform holds the keys and simply bills
   you for usage; you'd never touch a key. Bringing your own is the stepping
   stone, not the destination.)*
3. **You authorize access once.** The system must be granted permission to act
   on your GitHub, and only a human can grant that — it's a security boundary,
   not a step that can be automated away. One time.
4. **The one-time technical setup needs an admin.** Parts A & B below require
   someone with **GitHub org-admin rights** and basic GitHub comfort — *not* the
   vision owner. See "Who should do this."
5. **Your ongoing job is small and human:** bring ideas, and make the final call
   when the reviewers escalate. **The AIs never merge — you do.** That's the
   safety rail, on purpose.

Everything *mechanical* (repos, secrets, scaffolding, the code) is automatable
and goes away over time. The items above — **pay, authorize, decide** — are the
parts that are genuinely, permanently yours.

---

## ⚠️ Who should do this (read first)

This is a **one-time technical setup** per project. It is **not** for the
"vision owner" — it's for whoever manages your GitHub. The person running this
needs:

- **Admin access to the `NFS-247` GitHub organization** (to create repos, set
  org secrets, and change org Actions settings).
- Basic comfort **navigating GitHub Settings** — creating a repo, adding a file,
  pasting in secrets. *(No coding required, but you need to find your way around
  GitHub's settings pages.)*
- The **API keys** on hand: Anthropic, OpenAI, and Gemini.

If that isn't you, hand this document to your developer or whoever owns the
GitHub org. **Tech-stack provisioning and repo creation are real prerequisites
— don't expect a non-technical person to get through Part A or B alone.**

There are exactly **two settings that always require a human with org-admin
rights** (no tool/script can do them): repository **visibility** and org
**Actions permissions**. Everything else can be automated later.

---

## Part A — One-time org setup (do once for the whole org)

You only ever do this once. After it's done, every future project inherits it.

1. **Org Actions policy** → `NFS-247` org → Settings → Actions → General →
   **"Allow all actions and reusable workflows"** → Save.
2. **Org Workflow permissions** → same page, scroll to **"Workflow permissions"**
   → **"Read and write permissions"** → Save.
   *(This is what lets the reviewer post comments/labels. Skipping it causes a
   `401` later.)*
3. **Make the engine reachable** → `NFS-247/ai-peer-review` must be usable by
   org repos. Keeping projects **inside the `NFS-247` org** is what makes this
   work cleanly — see the troubleshooting note about owners.
4. **Org secrets (shared keys)** → `NFS-247` org → Settings → Secrets and
   variables → Actions → **New organization secret**, granted to all repos:
   `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, and (optional)
   `GOOGLE_CHAT_WEBHOOK_URL`, `APPROVE_WEBAPP_URL`, `APPROVE_SIGNING_SECRET`,
   `OPERATOR_GITHUB_LOGIN`, `OPERATOR_EMAIL`, `RESEND_API_KEY`.

---

## Part B — Add a new project (repeat per repo, ~5 minutes)

1. **Create the repo in the `NFS-247` org** (not a personal account — see
   troubleshooting). Add a README so it isn't empty.
2. **Add the workflow file** at `.github/workflows/ai-peer-review.yml` — copy
   `templates/caller-workflow.yml` from this repo. Pin it to the **latest
   release tag** (e.g. `uses: NFS-247/ai-peer-review@v2`).
3. *(Optional)* **Add a config** at `.peer-review.json` in the repo root for
   that project's danger paths / rules. No config = generic safe defaults
   (everything unknown is treated as high-stakes). See
   `templates/peer-review.example.json` and `ai-peer-review.schema.json`.
4. **Add the one per-repo secret**: `DISPATCHER_VERDICT_SECRET` — a long random
   string (run `openssl rand -hex 32`). Each repo signs with its own. Then make
   sure the repo has access to the org secrets from Part A.
5. **Open a pull request** with any small change. Within a couple of minutes the
   reviewers post their verdicts on the PR. Done.

---

## Part C — Troubleshooting (the things that actually go wrong)

| Symptom | Cause | Fix |
|---|---|---|
| `Unable to resolve action … not found` | The engine repo is private/unreachable from the caller, **or** the pinned ref doesn't resolve | Keep the project in the `NFS-247` org so a private engine still works; pin to a **tag** (e.g. `@v2`), not a branch name containing slashes |
| `HTTP 401: Requires authentication` on labels/comments | Token is read-only | Part A step 2 — org **Workflow permissions → Read and write** |
| No reviews; a "secret missing" notice | `DISPATCHER_VERDICT_SECRET` isn't set | Part B step 4 |
| A reviewer "could not be reached" | Missing/!wrong API key for that reviewer | Check the org secret for that provider (Anthropic/OpenAI/Gemini) |
| Endless auth/visibility pain | Repo was created under a **personal account** instead of the org | Recreate (or transfer) the repo into the `NFS-247` org — cross-owner setups fight you at every step |

---

## Billing modes (per tenant)

Each tenant is metered on every AI call (provider · model · tokens · cost),
recorded to a durable, signed **usage & billing ledger** in the repo. Two modes,
set in `.peer-review.json` (or the action inputs):

- **`byok`** *(default)* — the tenant brings their own keys and pays the
  providers directly. The platform charges nothing for usage (only an optional
  flat `dev_fee_usd`).
- **`platform`** — the platform's keys run it; the tenant is billed for usage at
  `usage_markup_multiplier` (e.g. `1.3` = 30% margin), plus `dev_fee_usd`.

The dispatcher only **emits** accurate, attributed usage — the actual invoicing
(reading the ledger, charging cards) is a separate billing service. For real
billing, that service should collect usage into an **NFS-controlled datastore**,
not rely on the per-repo ledger (which is convenient + tamper-evident, but lives
in the tenant's own repo).

## Controlling AI cost (models, prices, ceilings)

Spend is metered as **tokens × the price of the model actually in use**, summed
into a rolling 24-hour ledger that drives `daily_cost_ceiling_usd`. Two knobs,
both set as **org or per-repo Actions variables** (Settings → Secrets and
variables → Actions → *Variables*) so a value set once at the org applies
everywhere and any repo can override it:

- **Pick a cheaper model per provider** — `ANTHROPIC_MODEL`, `OPENAI_MODEL`,
  `GEMINI_MODEL`. Defaults are the strongest/priciest (`claude-opus-4-7`,
  `gpt-5`, `gemini-2.5-pro`). For most review work a mid-tier model is plenty and
  far cheaper — e.g. `ANTHROPIC_MODEL=claude-sonnet-4-6` cuts Claude's per-round
  cost ~5×, `GEMINI_MODEL=gemini-2.5-flash` cuts Gemini's ~4×. The price tracks
  the model automatically.
- **Pin exact prices** (optional) — `ANTHROPIC_INPUT_PRICE_PER_M` /
  `ANTHROPIC_OUTPUT_PRICE_PER_M` and the `OPENAI_…` / `GEMINI_…` equivalents
  (USD per 1M tokens). The built-in table holds published list rates; set these
  to **your actual contracted rates** and the ledger becomes exact. This is the
  fix for a daily ceiling that trips when real spend is low: a wrong price (too
  high) inflates the ledger and pauses reviews you'd never actually have paid
  that much for.

You don't have to touch either — the defaults work. They exist so the 24h ledger
reflects *real* money. Two safety rails ride on top: a **one-time Chat ping at
`daily_cost_warn_fraction`** (default 80%) of the ceiling so you can throttle
before reviews pause, and a **per-model spend breakdown** on that warning and on
the ceiling escalation so you can see *which* model is driving the bill.

## How the pieces fit (one paragraph)

`NFS-247/ai-peer-review` is the **shared engine**. Every project is its own repo
that plugs into it with the one `uses:` line in its workflow file. Projects
don't connect to each other — they each independently call the same engine, and
each carries its own `.peer-review.json` rules and its own verdict secret. Add a
repo, it gets reviewed; that's the whole model.
