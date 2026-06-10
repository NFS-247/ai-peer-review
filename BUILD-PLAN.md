# Front Door — Build Plan (the next cut)

The blueprint for turning the vision in `VISION.md` into a working product. It's
written so a fresh session, in the front-door repo, can execute it without
re-deriving the design.

> **One-line goal of the first cut:** a person types an idea in plain English and
> a **reviewed pull request** appears — built by an agent, torn apart by the four
> brains, waiting on the human's decision.

---

## Where it lives

A **new repo** — e.g. `NFS-247/idea-dispatcher` — separate from the review
engine. The review engine (`NFS-247/ai-peer-review@v2`) stays exactly as is; the
front door is a *consumer* of it, same as any tenant. (This session can't create
or build that repo — it's scoped to `ai-peer-review`. Create it, start a session
on it, hand it this file.)

---

## Architecture (the whole picture)

```
[ UI / front door ] ── talk, status, approvals
        │
        ▼
[ Orchestrator ] ── the conversation: idea → questions → VISION ARTIFACT
        │
        ├─► [ Provisioner ]  (GitHub App) ── create repo, wire @v2, scaffold
        │
        ▼
[ Build agent ] ── Claude Agent SDK: vision artifact → code → opens a PR
        │
        ▼
[ Review engine ]  NFS-247/ai-peer-review@v2  (BUILT — Claude+GPT+Gemini)
        │
        ▼
[ Operator decisions ] ── escalations surface in the UI / phone; human approves
```

Six components. **One is already built** (the review engine). The build agent is
mostly *wiring* (the Agent SDK already writes code and opens PRs). The
orchestrator, provisioner, UI, and billing-readout are the new work.

---

## The first cut (MVP) — the "build brain"

Build the **smallest end-to-end slice** and nothing more. Defer the UI,
self-provisioning, and billing to later milestones.

**Scope of cut 1:**
1. **Intake → vision artifact.** A conversational orchestrator (start as a CLI or
   a thin chat endpoint — *not* a polished UI yet) that interviews the user about
   an idea and emits a structured **vision artifact** (markdown/JSON: goal,
   scope, non-negotiables, acceptance criteria).
2. **Build agent.** Feed that artifact to the **Claude Agent SDK** running in a
   **pre-existing repo already wired to `@v2`** (skip provisioning for now). It
   implements the idea on a branch and opens a PR.
3. **Review = free.** The PR triggers the existing v2 engine automatically. No
   new code — that pillar is done.
4. **Decision loop.** Escalations show up where the operator already gets them
   (PR comment + Chat). For cut 1, the human approves on GitHub directly.

**Definition of done for cut 1:** type an idea → a branch + PR appear → the four
brains review it → the human approves/merges. No UI, no auto-repo-creation yet.

---

## Tech choices (recommended)

- **Build agent + orchestrator:** the **Claude Agent SDK** (the same thing
  driving the dev work you've already seen). It can plan, write code, run tests,
  and open PRs headlessly.
- **Provisioner (milestone 2):** a **GitHub App** installed once on the org —
  creates repos, commits the caller workflow + `.peer-review.json`, sets branch
  protection by API. (Secrets still need a human step or `gh`; the GitHub API
  for Actions secrets requires per-repo key encryption.)
- **UI (milestone 3):** a thin web app — a chat pane for intake, a board of
  in-flight projects, and an approvals inbox. Start boring; the value is the loop
  behind it, not the chrome.
- **Billing readout (milestone 4):** a service that reads each tenant's
  `usage & billing ledger` (already emitted by `@v2`) and invoices via Stripe.

---

## Milestones

| # | Milestone | What it proves |
|---|-----------|----------------|
| **1** | **Build brain** (above) | Idea → reviewed PR, end-to-end, no UI |
| 2 | **Self-provisioning** | New project wires itself; no human repo setup |
| 3 | **Front-door UI** (spec'd in `UI-PLAN.md`) | "Talk to one place" — intake + status + approvals |
| 4 | **Billing readout** | Per-tenant invoices off the usage ledger |

Cut 1 is milestone 1. Each later milestone is independently valuable and shippable.

---

## What this session already did toward it

- The **review engine** (milestone 0) is built, proven, and live at `@v2`.
- `VISION.md` is the spec the orchestrator builds the vision artifact toward.
- The **usage ledger** `@v2` already emits is exactly what milestone 4 reads.

## To start milestone 1

1. Create `NFS-247/idea-dispatcher` (empty).
2. Start a Claude Code session on it.
3. Hand it: this file + `VISION.md` (both public at
   `raw.githubusercontent.com/NFS-247/ai-peer-review/main/...`), and say:
   *"Build milestone 1 (the build brain) per BUILD-PLAN.md."*
