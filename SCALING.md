# Scaling to many tenants — GitHub rate limits

How the platform stays under GitHub's API limits as the number of users grows.
Limits are **per-identity**, some **per-repo**, some **per-installation** — so the
whole strategy is two ideas: **don't funnel everyone through one bucket**, and
**call GitHub less**.

## The limit model (the numbers that matter)

| Identity | Limit | Who uses it |
|---|---|---|
| Actions `GITHUB_TOKEN` | **~1,000 req/hr per repo** (15k on Enterprise Cloud) | the engine, today |
| User / OAuth token | 5,000 req/hr per user | the front door's writes (per-operator) |
| **GitHub App installation** | **5,000–15,000 req/hr, per installation** | the target for both, at scale |
| Secondary (anti-burst) | ~100 concurrent; content-creation throttle | everything |

The thing that bit us was the **per-repo `GITHUB_TOKEN` 1k/hr** ceiling on a busy
repo — not a global wall. It does **not** compound across tenants (separate repos
= separate buckets).

## Done — engine-side levers (live on `@v2`)

Shipped (`github_api.py` / `main.py`): the cheap, set-and-forget wins.
- **Labels from the PR list** — the scheduled sweep and pause-all read labels out
  of the one `pulls` list response instead of a `list_labels` call per PR
  (`list_open_pulls_with_labels`): O(1+N) → O(1). The sweep was the prime
  offender.
- **Memoized ledger lookup** — `find_issue_by_marker` learns the issue number
  once per run, then fetches it directly (fresh body) instead of re-listing every
  open issue on each of the several spend-ledger touches per round.
- **Retry/backoff** — `_request` now retries transient rate limits (429,
  rate-limit 403, 503) with capped backoff honoring `Retry-After` /
  `X-RateLimit-Reset`, so a momentary limit no longer fails a round. Writes are
  only retried when GitHub rejected them pre-processing (never double-posts).

These alone would have prevented the blowout. The structural moves below are for
*many tenants*, not a single busy repo.

## Move 1 — GitHub App: give every tenant their own bucket

Replace the per-repo `GITHUB_TOKEN` / shared PAT with **one GitHub App**, which
each tenant **installs** on their repos.

- Each installation has its **own 5,000–15,000 req/hr** quota → total capacity
  grows with tenants, and a noisy tenant can't starve the others.
- Raises the per-repo ceiling well above the 1k `GITHUB_TOKEN` cap.
- Also isolates **Actions concurrency** and gives clean **per-tenant attribution**
  for billing.

**How it plugs in:** the App authenticates with a short-lived JWT (App private
key) → exchanges it for a per-installation token (`POST
/app/installations/{id}/access_tokens`) → that token is what `GitHubAPI` and the
front door use for that tenant. The engine's workflow would request the App token
instead of `GITHUB_TOKEN` (a small auth swap at the top of the run); the front
door mints an installation token per tenant on demand (cache it ~50 min).

**Engine half — built (`scripts/dispatcher/github_app.py`).** A stdlib-only RS256
JWT + installation-token minter (no third-party crypto) is in place: set the
`app_id` / `app_private_key` action inputs (env `GITHUB_APP_ID` /
`GITHUB_APP_PRIVATE_KEY`) and the dispatcher authenticates via
`GitHubAPI.from_app` instead of the `GITHUB_TOKEN`. Still to do: register the App
and grant its repo permissions, and point the front door's board/approval reads
at the same minter — they ride the operator's shared user bucket today, the limit
that bit us.

**One-time human step (unavoidable, by design):** the tenant clicks **Install**
to grant repo access — GitHub requires that consent. It folds into onboarding as
a single button; **Milestone 2 (the provisioner) automates everything around it**
(repo creation, workflow + `.peer-review.json`, branch protection via the App).

## Move 2 — Front door: webhooks + a read-model, not polling

The board today reads GitHub live, per page view, across every repo —
`tenants × PRs × calls`. That's the real time bomb at scale.

- Register a webhook (the same App) so GitHub **pushes** PR/label/comment/issue
  events to the front door.
- Maintain a small **read-model (cache/DB)**; the board serves from it. GitHub is
  touched only on a **write** (an approval) or a periodic reconcile.
- The pure `viewmodels.py` already transform raw GitHub JSON into board/inbox
  rows — for this move, only the *source* changes (webhook payload instead of a
  live fetch). The tested core is reused as-is.

This is the "no DB for cut-1 → add a webhook-fed cache at scale" step flagged in
`UI-PLAN.md`.

## Hygiene (ongoing)

- **ETags / conditional requests** — a `304 Not Modified` doesn't count against
  the limit (biggest payoff in the polling/reconcile paths).
- **GraphQL / batching** — collapse many REST calls into one query.
- **Bounded sweep** — already a single filtered pass, not a walk-every-PR loop.
- **Backpressure** — read `X-RateLimit-Remaining/Reset` and slow *before* zero;
  spread content-creation to dodge the secondary (anti-burst) limits.

## When to do what (don't over-build early)

| Stage | Do |
|---|---|
| A few repos (now) | Nothing more — the engine levers (live) cover it. |
| Multiple tenants | **Move 1** (GitHub App). This is the big one; plan it with the provisioner (M2). |
| Board feels slow / many repos | **Move 2** (webhook read-model). |
| Heavy reconcile traffic | Add ETags + GraphQL. |
