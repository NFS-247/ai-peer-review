# Deploying the Front Door

A small **stateless** web service. Stdlib-only (no `pip install`), and **GitHub is
the store** (no database for cut-1), so "deploy" is just *a container with a few
env vars*.

## Fastest path — run it tonight (local, no OAuth)

Three env vars and one command. You see your real PRs and can approve/block from
the inbox immediately (writes use your dev token as the operator identity):

```bash
export GITHUB_READ_TOKEN=ghp_xxx                       # a token that can read the repos
export FRONT_DOOR_REPOS=NFS-247/StockTrader,NFS-247/Canary
export FRONT_DOOR_DEV_TOKEN=ghp_xxx                    # your token = operator for writes
python front_door/run.py                               # http://127.0.0.1:8000
```

That's the whole loop, usable, on your laptop. Everything below is to put it on a
URL for other people.

## Container

```bash
docker build -t front-door front_door/                 # build context = front_door/
docker run -p 8000:8000 \
  -e GITHUB_READ_TOKEN=ghp_xxx \
  -e FRONT_DOOR_REPOS=NFS-247/StockTrader \
  -e FRONT_DOOR_DEV_TOKEN=ghp_xxx \
  front-door
```

The image has no dependencies to install, so builds are seconds. `/healthz` is the
container health check (already wired in the Dockerfile).

## Env vars

| Var | Required | Purpose |
|---|---|---|
| `GITHUB_READ_TOKEN` | yes | reads the board (org App install token or a PAT) |
| `FRONT_DOOR_REPOS` | yes | comma list `owner/repo,…` to show |
| `FRONT_DOOR_DEV_TOKEN` | dev | operator token for writes (skip in prod once OAuth is on) |
| `GITHUB_OAUTH_CLIENT_ID` / `_SECRET` | prod | GitHub OAuth app credentials |
| `FRONT_DOOR_PUBLIC_URL` | prod | e.g. `https://app.example.com` (→ Secure cookies + OAuth callback) |
| `PORT` | auto | injected by the PaaS; honored automatically |

## Production (hosted, with GitHub OAuth)

1. **Register a GitHub OAuth App** — Authorization callback URL =
   `<FRONT_DOOR_PUBLIC_URL>/auth/callback`. Copy the client id/secret.
2. **Deploy the container** on any PaaS (the Dockerfile is host-agnostic):
   - **Cloud Run:** `gcloud run deploy front-door --source front_door/ --allow-unauthenticated`
   - **Render / Railway:** point at the repo, Docker env, set the env vars.
   - **Fly.io:** `fly launch` in `front_door/` (Dockerfile detected), `fly secrets set …`.
3. **Set env** (the OAuth set + `GITHUB_READ_TOKEN` + `FRONT_DOOR_REPOS` +
   `FRONT_DOOR_PUBLIC_URL`); drop `FRONT_DOOR_DEV_TOKEN`. Users sign in at `/login`.

A public **HTTPS** URL is required for the OAuth callback.

## Self-serve onboarding (the Connect page)

`/connect` lets a signed-in user wire **their own** repo for review in one form:
check the AI providers they want, paste each one's key, name the repo, submit.
There is **no GitHub App to register** — provisioning runs as the signed-in user
over the OAuth token they already have. That token's default `repo` scope is what
covers it: creating the repo, committing the workflow (`@v3`) + `.peer-review.json`,
and setting the Actions secrets. The selected providers drive both the secrets and
the reviewer roster, so a repo never requires a model it has no key for.

Requirements for Connect specifically:

- **OAuth configured** (prod), so each user provisions as themselves. (In local
  dev-token mode it works too, as the single dev user.)
- **PyNaCl installed** — the container already installs it (`requirements.txt`);
  it's what encrypts each secret with a libsodium sealed box before it's sent.
  Without it, Connect returns a clear "install PyNaCl" error instead of provisioning.

The user's only steps are the ones you want them to have: make a GitHub account,
buy their AI keys, sign in, paste the keys. Org-owned repos and connecting a
pre-existing repo work; creating under the user's own account is the default.

## Notes for scale

- **Sessions are in-memory** → run a single instance, or use sticky sessions, or
  swap the `SessionStore` for Redis/DB (same interface) when you go multi-instance.
- **Board freshness** is live-read today; at many repos move to the webhook-fed
  read-model in `../SCALING.md` (Move 2).
- **Keep it on its own origin**, separate from any privileged internal hub — see
  the trust-zone reasoning in the platform docs. The front door is the
  customer-facing product; it should not share a domain with admin systems.
