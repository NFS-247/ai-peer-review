# Front Door (Milestone 3 UI) — portable package

The "talk to one place" surface from `VISION.md` / `BUILD-PLAN.md` / `UI-PLAN.md`:
a board of in-flight projects, an approvals inbox, and (seam for) chat intake.

**This package was authored inside `NFS-247/ai-peer-review` but is meant to be
lifted into the front-door repo** (`NFS-247/idea-dispatcher` or a dedicated
`front-door`). It is a *consumer* of the review engine — it reads the engine's
state from GitHub and writes operator decisions back — and adds **no** review
logic. It is deliberately **stdlib-only** so it runs and tests anywhere; swap the
thin web layer (`app/server.py`) for Next.js/FastAPI later without touching the
tested core (`app/viewmodels.py`, `app/commands.py`, `app/gh.py`).

## What's built (UI-PLAN sub-slices)

- **3a — project board** (`/`): every tenant repo's open PRs with tier, round,
  status, cost-so-far, 24h spend + per-model split, and reviewer verdicts. Pure
  reads.
- **3b — approvals inbox** (`/inbox`): the actionable queue (ready / escalated)
  with one-tap **Approve / Block / Investigate** that post the matching
  `OPERATOR` command **as the logged-in operator** (the identity the engine
  requires).
- **3c — chat intake**: a documented seam (`app/router.py: intake_*`) to wire to
  the Milestone-1 orchestrator's API. Not built here (it needs M1's endpoint).

## The data contracts (read straight from the engine's GitHub state)

| Signal | Source |
|---|---|
| status | PR labels: `dispatcher:ready-for-merge` / `:escalated` / `:paused` / `:secret-missing` |
| tier / round | labels `dispatcher:tier-*` / `dispatcher:round-*` |
| cost-so-far | signed PR state comment `<!-- tradewatcher-dispatcher-state -->` → `cumulative_cost_usd` |
| 24h spend + per-model | issue `<!-- tradewatcher-dispatcher-global-spend -->` → `{ts,cost,by}` events |
| reviewer verdicts | ```tradewatcher-verdict``` blocks in PR comments |
| decisions (write) | a PR comment `OPERATOR <VERB>` authored by the operator |

## The one real constraint (auth)

The engine only honors an operator command **authored by that repo's configured
operator GitHub account**. So writes use the **operator's own token**:

- **Production — GitHub OAuth.** The user signs in (`/login` → GitHub →
  `/auth/callback`); their token is kept **server-side** in the session store
  (`sessions.py`), keyed by an opaque httponly `fd_sid` cookie — the token never
  reaches the browser. The OAuth handshake is state-protected, and action POSTs
  are **CSRF-protected** against the session. Each user is operator of their own
  repos — multi-tenant for free, no engine change.
- **Local dev — `FRONT_DOOR_DEV_TOKEN`.** No OAuth app needed; that token is the
  operator identity and CSRF is skipped (single-user localhost).

Board *reads* use a separate `GITHUB_READ_TOKEN` (an org App install or PAT).

## Run it

**Dev (no OAuth):**
```bash
export GITHUB_READ_TOKEN=ghp_...          # reads the board (repo: read)
export FRONT_DOOR_REPOS=NFS-247/StockTrader,NFS-247/Canary
export FRONT_DOOR_DEV_TOKEN=ghp_...        # operator token for writes (dev only)
python front_door/run.py                   # serves http://127.0.0.1:8000
```

**Production (GitHub OAuth):** register an OAuth App with callback
`<public-url>/auth/callback`, then also set:
```bash
export GITHUB_OAUTH_CLIENT_ID=...
export GITHUB_OAUTH_CLIENT_SECRET=...
export FRONT_DOOR_PUBLIC_URL=https://app.example.com   # https -> Secure cookies
# (drop FRONT_DOOR_DEV_TOKEN; sign in via /login)
```
The in-memory session store is fine for one instance; for multi-instance, swap it
for Redis/DB behind the same `SessionStore` interface.

## Test it

```bash
python -m pytest front_door/tests -q
```

## Lifting it into the front-door repo

Move the `front_door/` directory to the new repo's root (drop the `front_door.`
import prefix or keep it as the package). The tested core travels unchanged; only
`config.py` (which repos/tokens) and the web layer are environment-specific.
