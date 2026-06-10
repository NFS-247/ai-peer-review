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
   ceiling escalates to the operator (GitHub comment + optional email).
4. **Never merges.** It has no `contents: write`. The human merges.

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
   `uses: NFS-247/ai-peer-review@v1` — the dispatcher code is fetched
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

Consuming repos pin a tag (`@v1`). Cutting a new dispatcher version is a
deliberate tag bump, so a change here can never silently change how an existing
project's PRs are reviewed.

## Local development

```
python -m pytest tests/ -q     # 150 tests, no dependencies to install
```
