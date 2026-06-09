# One-tap approve from Google Chat

Turns the escalation card's button into **✅ Approve** — tap it and the
dispatcher gets your `OPERATOR APPROVE`, no typing. It's a small Google Apps
Script locked to your own Google account. One setup, works for every project.

## How it works

```
PR needs you → dispatcher posts Chat card with "✅ Approve" button
   → you tap it → opens this Apps Script (only YOU can open it)
   → it posts "OPERATOR APPROVE" on the PR as you
   → shows a "done → merge here" page → you tap Merge on GitHub
```

You still do the final **Merge** tap yourself (approve-only), by design.

## Setup (once, ~10 min)

### 1. Make a GitHub token for it
GitHub → **Settings → Developer settings → Fine-grained personal access tokens →
Generate new token**:
- **Resource owner:** NFS-247
- **Repository access:** the repos you want to approve from Chat (StockTrader, etc.)
- **Permissions → Repository → Pull requests: Read and write**
- Generate, copy the token (starts `github_pat_…`).

### 2. Create the Apps Script
- Go to **script.google.com → New project**.
- Delete the sample, paste the contents of **`Code.gs`** (next to this file).
- **Project Settings (gear) → Script properties → Add script property:**
  | Property | Value |
  |----------|-------|
  | `GITHUB_TOKEN` | the `github_pat_…` from step 1 |
  | `GITHUB_OWNER` | `NFS-247` |
  | `APPROVE_TOKEN` | *(optional)* a long random string for extra safety |

### 3. Deploy it as a web app
- **Deploy → New deployment → ⚙ → Web app.**
- **Execute as:** Me
- **Who has access:** **Only myself**  ← this is the lock; nobody else can trigger it
- **Deploy**, authorize when prompted, and **copy the Web app URL** (ends in `/exec`).

> If you set `APPROVE_TOKEN`, append `?token=YOURSTRING` to that URL before using it.

### 4. Give the repos the URL
For each repo (StockTrader, …): **Settings → Secrets and variables → Actions →
New repository secret**:
- **Name:** `APPROVE_WEBAPP_URL`
- **Value:** the `/exec` URL from step 3

That's it. Next time a PR escalates, the card shows **✅ Approve** — one tap.

## Security

- The deployment is **"Only myself"**, so only you (signed into your Google
  account) can ever invoke it. A leaked link does nothing for anyone else.
- The GitHub token lives in **Script properties**, never in the card or code.
- Optional `APPROVE_TOKEN` adds a shared-secret check on top.
- The dispatcher registers `APPROVE_WEBAPP_URL` as a secret value, so it is
  scrubbed from any comment the dispatcher posts.
