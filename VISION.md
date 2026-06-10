# The Idea Dispatcher — Platform Vision

> You bring the ideas. Four AI brains build, review, and guard them.
> You only decide. Everything mechanical disappears.

This is the north-star vision. The AI peer-review engine in this repo is its
first built pillar — the part that's live today. This document is the thing that
comes *before* development: the vision the build follows.

---

## The shape of it

A person with an idea — not a developer — talks to one place. They describe what
they want. Software gets built, reviewed by independent AIs, and shipped, with
the person pulled in only to make the calls that are genuinely theirs. They never
set up a repo, write code, manage keys, or touch infrastructure.

**The four brains:**
- **Claude builds** — turns a vision into code and opens pull requests.
- **GPT and Gemini review** — tear the work apart adversarially; no rubber-stamping.
- **The dispatcher referees** — runs the reviewers, converges agreement, and
  escalates only when there's a real decision to make.

**The fifth thing they orbit is you:** ideas in, final call out.

---

## One idea's journey

1. **You describe an idea** in a UI — plain English. You're talking to the
   orchestrator (Claude), which asks sharp questions until the idea is real
   enough to build against. *This is the work: building the vision before any
   code exists.* The output is a durable **vision artifact**.
2. **The project provisions itself** — a repo is created, wired to the review
   engine, scaffolded. You see none of it.
3. **A build agent writes the first version** and opens a PR.
4. **The four brains review it** against the vision artifact — not just "is this
   correct," but "does it serve the vision, did it break a non-negotiable."
5. **You're pulled in only when it matters** — a disagreement, a high-stakes
   change — and you approve, block, or dig in. **The AIs never merge. You do.**
6. **You iterate** — "now add Y" — and it runs the loop again.

The vision artifact does three jobs at once: it's the **spec** the build agent
develops from, the **rubric** the reviewers judge against, and the thing **only
you can change** — so drift escalates to you, the one allowed to amend it.

---

## What's built today (the review pillar)

- A **multi-tenant review engine** (`@v2`): every project plugs in with one line
  (`uses: NFS-247/ai-peer-review@v2`) and gets Claude + GPT + Gemini review,
  tuned per project by a `.peer-review.json`.
- **Proven** end-to-end on two repos (the canary + StockTrader in production),
  with the safety rails firing for real (spend ceilings, graceful degradation,
  escalate-don't-merge).
- A **billing foundation** — per-tenant usage metering, `byok` vs `platform`
  modes, and a margin hook.
- A **delegable onboarding runbook** so adding a project is minutes, not a saga.

## What's next (the build pillar + the front door)

- **The build brain** — idea → code → PR, automatically (this is what Claude
  Code / the Agent SDK already does; it's wiring, not inventing).
- **Self-provisioning** — a GitHub App installed once that creates repos, wires
  them, and sets secrets by API, so "set up a repo" disappears.
- **The UI / front door** — the "talk to one place" experience that ties intake,
  build, review, and decisions together.

---

## The business model

- **`platform` mode** — the platform (NFS) holds the API keys, pays the
  providers, and bills each tenant for usage with a margin. The user never
  touches a key.
- **`byok` mode** — the tenant brings their own keys and pays providers
  directly; the platform charges only a service/dev fee.
- Usage is metered per tenant from day one; invoicing (Stripe, etc.) is a
  separate service that reads the ledger.

## What is permanently the user's (and only the user's)

Everything mechanical is automatable and goes away. Three things never do —
because they're about money, trust, and judgment:

1. **Pay** — authorize money once; charges are automatic after.
2. **Authorize access** — grant the system permission once (a security boundary).
3. **Decide** — bring ideas, and make the final call on anything high-stakes.
   This isn't friction — it's the safety rail. The AIs never merge; you do.

---

## Principles

- **Vision before development.** The idea becomes a durable artifact first;
  everything downstream builds and is judged against it.
- **The human is the safety rail, not the operator.** AIs propose; the human
  disposes. No AI ever merges, deploys, or moves money on its own.
- **Honest up front.** Before anyone pours in an idea, they're told exactly what
  it costs and what's required of them. No bait-and-switch.
- **Adversarial by design.** Independent reviewers that disagree catch what one
  agreeable assistant misses.
