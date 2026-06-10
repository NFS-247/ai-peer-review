# Front Door — UI Build Plan (Milestone 3)

The executable spec for the **front-door UI**: the "talk to one place" surface
where a person starts an idea, watches the four brains work, and approves the
result — without ever opening GitHub. Written so a fresh session, **in the
front-door repo**, can build it without re-deriving the design.

It builds on:
- `VISION.md` — the north star (idea → build → review, four brains).
- `BUILD-PLAN.md` — the six-component architecture; the UI is component 1.
- The **review engine** (`NFS-247/ai-peer-review@v2`), already live. The UI is a
  *reader* of the engine's state and a *writer* of operator decisions — it adds
  no review logic.

> **One-line goal:** an operator opens one web app and sees every in-flight
> project, approves or blocks from an inbox, and starts a new idea by chatting —
> the GitHub PRs, labels, and ledgers are the backend they never have to see.

---

## Where it lives

The front-door repo (e.g. `NFS-247/idea-dispatcher`, or a dedicated
`NFS-247/front-door` web app). **Not** this repo — the engine stays a headless
consumer-agnostic action. The UI talks to two things: the **orchestrator /
build-brain** (Milestone 1, intake + status) and **GitHub** (the engine's state).

---

## The engine's state IS the backend (data contracts)

Do **not** build a parallel database for cut 1. The engine already persists
everything the UI needs in GitHub, with stable, documented markers. Read these:

**Per-PR review state** (via the GitHub REST API on each tenant repo):
- Labels — the at-a-glance status:
  - `dispatcher:tier-<routine|backend|high_stakes>` — classification
  - `dispatcher:round-<n>` — how many review rounds so far
  - `dispatcher:ready-for-merge` — converged; waiting on the human merge click
  - `dispatcher:escalated` — needs an operator decision
  - `dispatcher:paused` — operator paused, or a project-wide spend stop
  - `dispatcher:secret-missing` — misconfig (show as a setup error)
- Reviewer verdicts — one signed PR comment per reviewer per round. Parse the
  `approve` / `request_changes` verdict + reasoning for the per-reviewer view.
- Cross-run state — a single signed PR comment marked
  `<!-- tradewatcher-dispatcher-state -->`, JSON with `cumulative_cost_usd`,
  `pending_escalation_*`, `escalated_head_sha`, `ci_fix_attempts`. The
  per-PR cost-so-far for the board comes from here.

**Per-repo spend & billing** (tracking issues, found by marker):
- 24h spend ledger — issue marked `<!-- tradewatcher-dispatcher-global-spend -->`
  ("[dispatcher] global spend ledger"). Events are `{ts, cost, by:{provider:usd}}`
  → drives the cost gauge and the per-model breakdown.
- Usage & billing ledger — issue marked
  `<!-- tradewatcher-dispatcher-usage-ledger -->` ("[dispatcher] usage & billing
  ledger"), a signed `UsageSummary` with `by_provider` → drives the billing
  readout (Milestone 4 reads the same thing).

**Mobile signal already emitted:** the engine posts Google Chat cards
(escalation / ready / budget warning). The UI's approvals inbox is the same
content as those cards, in a web queue — reuse the card copy as the item
template.

---

## The decision path (the one real constraint — read this twice)

An operator command only counts when the engine sees it **authored by that
repo's configured operator GitHub account**. `main._handle_operator_command`
validates `comment.author_id == operator_user_id`; a command from anyone else
(or a bot) is ignored. So the UI cannot "approve" with a generic service token.

**Use GitHub OAuth.** The operator signs into the UI with their GitHub account;
the UI writes the `OPERATOR <VERB>` comment with *their* OAuth token, so it
carries their identity natively. This also makes the platform multi-tenant for
free: each user is the operator of their own repos, no shared bot identity, and
**no engine change** (avoid teaching the dispatcher to trust an extra identity —
that would widen the trust boundary the signed-verdict design deliberately keeps
narrow).

The command vocabulary (first line of the comment, uppercase):
`OPERATOR APPROVE` · `OPERATOR BLOCK <reason>` · `OPERATOR INVESTIGATE <note>` ·
`OPERATOR DISCUSS <note>` · `OPERATOR PAUSE` · `OPERATOR RESUME` ·
`OPERATOR KILL`. (Approve does **not** merge — the human still clicks merge, by
design. A "🚀 Approve & Merge" affordance can reuse the engine's existing signed
`build_approve_url` / `sign_action` one-tap path.)

---

## The three surfaces

**A. Project board** (read-only). Every project (repo) and its open PRs, each
row: title, tier, round, status (from labels), cost-so-far (state comment), 24h
spend + per-model split (global ledger), reviewer verdicts. Pure aggregation
over the GitHub API — immediately useful as a dashboard, writes nothing.

**B. Approvals inbox** (the high-value loop). The actionable queue: PRs labeled
`dispatcher:ready-for-merge` or `dispatcher:escalated`. Each item shows the
escalation reason, reviewer summaries, and the 24h spend breakdown, with one-tap
**Approve / Approve&Merge / Block / Investigate** that post the matching
`OPERATOR` command as the logged-in operator (section above). "Your phone, but a
real inbox."

**C. Chat intake pane** (closes the loop). A chat pane wired to the Milestone-1
orchestrator endpoint: describe an idea → it interviews you → emits the vision
artifact → kicks the build agent → a PR appears and shows up on the board. New
idea = new project.

---

## Tech choices (recommended, not mandatory)

- **App:** a thin web app (Next.js/React, or server-rendered — boring is fine).
- **Reads:** the org **GitHub App** installation token (board + ledgers across
  all tenant repos).
- **Writes (operator commands):** **per-user GitHub OAuth** (identity, per the
  decision-path constraint).
- **Freshness:** poll the GitHub API (30–60s) for cut 1; later, subscribe to the
  same webhooks the engine already reacts to for push updates.
- **Store:** none for cut 1 — GitHub is the source of truth. Add a thin read
  cache/index only if board latency demands it.

---

## Build order (smallest valuable slice first)

| Sub | Slice | Proves |
|-----|-------|--------|
| 3a | **Project board** (read-only) | The data contracts above resolve to a live dashboard |
| 3b | **Approvals inbox** + OAuth operator commands | The decide-from-one-place loop, end to end |
| 3c | **Chat intake** wired to the M1 orchestrator | "Talk to one place" — idea starts in the UI |

Each sub-slice is independently shippable. 3a is useful the day it lands.

**Definition of done (Milestone 3):** an operator opens the UI, sees every
in-flight project and its review state, approves/blocks/investigates from the
inbox (commands land as them and the engine reacts), and starts a new idea from
the chat pane — without opening GitHub.

---

## Reuse, don't rebuild

- The engine's **labels + ledger issues** are the data model — read them, don't
  mirror them.
- **`build_approve_url` / `sign_action`** (in the engine's `call_google_chat`)
  already produce signed one-tap approve/merge links — reuse for Approve&Merge.
- The **usage ledger** is Milestone 4's billing source — the board's cost column
  and the future invoice read the same issue.
- The Google Chat **card copy** is the inbox item template — same words, same
  reasons, same buttons.

## Deferred / open decisions

- Hosting + auth app registration (GitHub App + OAuth app).
- Whether 3a needs a read index or stays GitHub-native (start native).
- Surfacing **billing** (Milestone 4) inside the board vs a separate view.
- Real-time: polling (cut 1) vs webhook push (later).

## To start Milestone 3

1. Be in the front-door repo (the same one as Milestone 1, or a dedicated UI
   repo that calls the orchestrator's API).
2. Hand a session: this file + `BUILD-PLAN.md` + `VISION.md` (all public at
   `raw.githubusercontent.com/NFS-247/ai-peer-review/main/...`), and say:
   *"Build Milestone 3 sub-slice 3a (the read-only project board) per
   UI-PLAN.md."*
