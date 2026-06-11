# ai-peer-review

An autonomous AI peer-review dispatcher for GitHub pull requests.

Claude builds code → **GPT and Gemini review it adversarially** → only
HMAC-signed verdicts emitted by this dispatcher count → the human operator
makes the final merge decision. No copy-paste between chat windows.

This repo is the **reusable** version: drop a tiny workflow into any repo and
it gets the same review system, tuned per project by a single JSON config.

---

## What it does

On every PR (and on CI completion, and on operator comments), the dispatcher:

1. **Classifies** the change into `routine` / `backend` / `high_stakes` from
   the file paths and a content scan of the diff. Unknown paths default to
   `high_stakes` (deny-first).
2. **Dispatches reviewers** for that tier (e.g. high-stakes → Claude + GPT +
   Gemini), each posting a **signed verdict**.
3. **Converges or escalates.** Agreement converges; disagreement, suspicious
   unanimity on a first high-stakes round, a hard round cap, or a spend
   ceiling escalates to the operator (GitHub comment + optional email + phone).
   Review-outcome escalations are **cooldown-gated**: the phone pings only once
   the dev agent has gone quiet (no new commit) for `escalation_cooldown_minutes`
   (default 10) — never mid-iteration. Infra/budget stops ping immediately.
4. **Pings you when it's ready.** On convergence the dispatcher posts a
   **"ready to merge" Chat card for every tier** (not just escalations), so a
   backend PR that quietly goes green still reaches your phone.
5. **Never merges.** It has no `contents: write`. The human merges.

Safety properties (see `scripts/dispatcher/README.md` and the design doc):

- **Fail-closed.** No `DISPATCHER_VERDICT_SECRET` → no verdict counts → it can
  never falsely approve.
- **Forgery-proof.** Only signed verdicts from the dispatcher's own bot author
  count. Pasting a verdict block into a comment does nothing.
- **Secret-redacting.** API keys are scrubbed at every comment/review choke
  point.
- **Runs trusted code only.** The dispatcher package runs from *this* repo at a
  pinned tag, never from the PR head under review.
- **Stdlib-only.** Zero pip installs in the workflow. `tests/test_dispatcher_standalone.py`
  fails the build if anyone reintroduces an external or sibling-package import.

---

## Onboarding a new repo (≈10 minutes)

1. **Add the caller workflow.** Copy `templates/caller-workflow.yml` into the
   target repo at `.github/workflows/ai-peer-review.yml`. It runs
   `uses: NFS-247/ai-peer-review@v2` — the dispatcher code is fetched
   automatically, no PAT or cross-repo checkout.

2. **(Optional) Add a config.** Copy `templates/peer-review.example.json` to
   `.peer-review.json` at your repo's **root** and edit it for the project's
   danger paths, head-lock paths, and rosters. (The legacy location
   `.github/ai-peer-review.json` is still loaded automatically, so existing
   tenants need no migration.) No config = generic safe defaults (everything
   unknown is high-stakes). The file is **JSON, not YAML** — the dispatcher is
   stdlib-only and ships no YAML parser. Field reference:
   `ai-peer-review.schema.json`. StockTrader's full gold-standard ruleset is in
   `templates/peer-review.stocktrader.json`.

3. **Secrets — share them at the org level (Cut 1).** For NFS-247's own repos,
   set one set of **organization** secrets (Settings → Secrets and variables →
   Actions → *New organization secret*) and grant the consuming repos access.
   Tenants no longer each need their own keys:

   | Secret | Scope | Required | Purpose |
   |--------|-------|----------|---------|
   | `ANTHROPIC_API_KEY` | org | for Claude reviews | Claude reviewer |
   | `OPENAI_API_KEY` | org | for GPT reviews | GPT reviewer |
   | `GEMINI_API_KEY` | org | for Gemini reviews | Gemini reviewer (high-stakes) |
   | `GOOGLE_CHAT_WEBHOOK_URL` | org | optional | mobile escalation + merge-ready pings |
   | `APPROVE_WEBAPP_URL` / `APPROVE_SIGNING_SECRET` | org | optional | one-tap approve (links are HMAC-bound per repo+PR, so sharing is safe) |
   | `OPERATOR_GITHUB_LOGIN` | org | recommended | who may issue `OPERATOR` commands |
   | `OPERATOR_EMAIL` / `RESEND_API_KEY` | org | optional | escalation email (falls back to a PR comment) |
   | `DISPATCHER_VERDICT_SECRET` | **per-repo** | **yes** | HMAC key; deliberately NOT shared — it's the cross-tenant forgery boundary, so each repo signs with its own |

   Why one exception: a **shared** verdict secret would let a signed verdict
   from one repo be replayed in another. Keep it per-repo. The 24h spend ledger
   is also naturally per-repo (it lives in a tracking issue in each repo), so
   projects never cross-contaminate on spend.

4. **Branch protection.** Require the PR + the project's test check, dismiss
   stale approvals. AI verdicts are comments, so keep "required approvals" at 0.

That's it. Open a PR and the reviewers show up.

---

## This repo's layout

```
action.yml                 the composite action consuming repos invoke (uses:)
scripts/dispatcher/        the dispatcher package (stdlib only)
tests/                     full test suite (run with: python -m pytest tests/ -q)
.github/workflows/
  selftest.yml             CI: runs the suite + portability guards on every change
templates/
  caller-workflow.yml      copy into a consuming repo
  peer-review.example.json copy to .peer-review.json (repo root) and edit
  peer-review.stocktrader.json  StockTrader's gold-standard ruleset (ported)
ai-peer-review.schema.json config field reference
```

## Versioning

Consuming repos track the **`@v2` branch**, so engine fixes — new escalation
cards, timeout tuning, security patches — reach every project automatically on
its next run, with no per-repo bump. The trade-off is deliberate: this org owns
all its consumers and wants fixes to propagate, rather than freezing each repo
on an old snapshot. (The earlier `@v1` *tag* did exactly that silently — repos
pinned to it never received a single update; that's the trap this replaces.) A
repo that genuinely needs to freeze can pin a specific commit SHA instead of
`@v2`.

## Cut 1 — multi-tenancy across NFS-247's repos

Cut 1 makes the dispatcher cleanly multi-tenant for NFS-247's **own** repos
(opening to outside tenants is Cut 2). What changed:

- **Generic defaults.** The dispatcher no longer hard-codes any one project's
  rules. Built-in defaults are generic and deny-first; each repo's
  `.peer-review.json` carries its own `high_stakes_paths`, safety tokens,
  `head_lock_paths`, reviewer prompt context (`project_description` /
  `review_guidance`), rosters and ceilings.
- **Shared org secrets** for the API keys / Chat webhook / approve webapp (the
  verdict secret stays per-repo — see onboarding above).
- **Cooldown escalation timing** so the phone never pings mid-iteration, plus a
  `schedule` sweep that delivers the ping once a PR goes quiet.
- **Merge-ready Chat ping for all tiers**, and a **rate-limit-resilient** approve
  button.

### Migrating StockTrader to Cut 1 (do this in order)

1. Commit `templates/peer-review.stocktrader.json` to **StockTrader's repo root**
   as `.peer-review.json`. It reproduces StockTrader's former hard-coded ruleset
   byte-for-byte. **Do this before step 2**, or StockTrader's strict rules
   revert to generic defaults until the file lands.
2. Re-pin StockTrader's caller workflow to the new dispatcher tag, and add the
   `on: schedule` trigger (copy from `templates/caller-workflow.yml`) so the
   cooldown sweep runs.
3. Move the shared API keys / `GOOGLE_CHAT_WEBHOOK_URL` / `APPROVE_*` to
   **org-level** secrets; keep `DISPATCHER_VERDICT_SECRET` per-repo.

### Adding the canary (or any new NFS-247 repo)

Copy `templates/caller-workflow.yml` → `.github/workflows/ai-peer-review.yml`
and (optionally) `templates/peer-review.example.json` → `.peer-review.json`,
then grant the repo access to the org secrets. No config = generic safe
defaults.

## Local development

```
python -m pytest tests/ -q     # full suite, no dependencies to install
```
