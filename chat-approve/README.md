# One-tap approve / merge from Google Chat

Turns the escalation card's buttons into **✅ Approve** and **🚀 Approve & Merge** —
tap and the dispatcher gets your decision, no typing. It's a small Google Apps
Script locked to your own Google account, with each link **signed for one
specific PR** so it can't be edited to act on another.

## How it works

```
PR needs you → dispatcher posts a Chat card with signed Approve / Approve&Merge
   → you tap → opens this Apps Script (only YOU can open it; signature checked)
   → it posts "OPERATOR APPROVE" as you (and merges, for Approve & Merge)
```

Two locks: the **"Only myself"** deploy (only your Google account can invoke it)
**and** a per-PR **HMAC signature** (a link only works for the exact PR + action
it was minted for — editing `pr=90` to `pr=99` is rejected).

**Rate-limit resilient:** if GitHub rate-limits the approve/merge call (HTTP
403/429), the script retries once with a short backoff (honoring `Retry-After`),
then — if still limited — shows a friendly **"tap to open the PR and approve
manually"** page instead of a scary `GitHub error 403`. Your tap is never lost.

## Setup (once)

### 1. GitHub token
GitHub → Settings → Developer settings → **Fine-grained personal access tokens**:
- **Resource owner:** NFS-247 · **Repos:** StockTrader (+ others later)
- **Permissions → Pull requests: Read and write**
- **Also Contents: Read and write** — required for the **Approve & Merge** button
- Copy the `github_pat_…`.

### 2. Pick a signing secret
Make one long random string (e.g. a password manager "generate"). You'll paste
the **same** value in two places below. Call it `SIGNING`.

### 3. Apps Script
- **script.google.com → New project** → paste `Code.gs`.
- **⚙ Project Settings → Script properties:**
  | Property (exact) | Value |
  |---|---|
  | `GITHUB_TOKEN` | the `github_pat_…` |
  | `GITHUB_OWNER` | `NFS-247` |
  | `APPROVE_SIGNING_SECRET` | your `SIGNING` string |
  | `MERGE_METHOD` *(optional)* | `merge` (or `squash` / `rebase`) |

### 4. Deploy
- **Deploy → New deployment → Web app** · Execute as **Me** · Access **Only myself**.
- Copy the **/exec URL**.

### 5. Repo secrets (StockTrader, then each project)
Settings → Secrets and variables → Actions:
| Secret (exact) | Value |
|---|---|
| `APPROVE_WEBAPP_URL` | the `/exec` URL |
| `APPROVE_SIGNING_SECRET` | the **same** `SIGNING` string from step 2 |

That's it. The next escalation card shows **✅ Approve** and **🚀 Approve & Merge**.

## Security

- **Only myself** deploy → only you (signed into Google) can invoke it.
- **Per-PR HMAC** → a leaked/edited link is rejected for any other PR or action.
- GitHub token lives only in **Script properties**.
- The dispatcher registers `APPROVE_WEBAPP_URL` and `APPROVE_SIGNING_SECRET` as
  secret values, so they're scrubbed from any comment it posts.
- Keep the Chat space **members = just you**, so the card (and its links) aren't
  shared even though the links are inert for anyone else.
