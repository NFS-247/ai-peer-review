"""Self-serve provisioning: wire a fresh repo to the AI peer-review engine.

BUILD-PLAN milestone 2 (the Provisioner). Given a GitHub-App-authenticated client
for the user's own account, this makes a repo review-ready in one shot:

  - commit the single caller workflow (.github/workflows/ai-peer-review.yml,
    pinned to @v3) and a minimal .peer-review.json (operator = the user),
  - set the repo's Actions secrets: the user's BYOK model keys + a freshly
    generated DISPATCHER_VERDICT_SECRET.

After this, any PR opened on the repo is reviewed by the engine and shows up in
the front-door board/inbox — the user never hand-edits config or secrets.

This module is the pure ORCHESTRATION: it talks only to a ``ProvisioningClient``
interface, so it is fully testable without GitHub. The concrete client (real HTTP
+ libsodium sealed-box secret encryption) is a separate adapter, and the
"install the App / paste your keys" UI is a separate surface — both build on this.
"""

from __future__ import annotations

import json
import secrets as _secrets
from dataclasses import dataclass
from typing import Mapping, Optional, Protocol


WORKFLOW_PATH = ".github/workflows/ai-peer-review.yml"
CONFIG_PATH = ".peer-review.json"
ENGINE_REF = "v3"

# Actions secrets for the AI model keys a tenant brings (BYOK — they pay the
# providers directly). Only the ones actually supplied are set.
MODEL_KEY_SECRETS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY")
# The default routine/backend panel is claude + gpt, so a repo can't converge
# without at least these two — require them up front rather than wiring a repo
# whose reviews would immediately escalate "required reviewer unavailable".
REQUIRED_KEY_SECRETS = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")

# Plain string (no f-string): GitHub Actions ${{ ... }} must stay literal, so the
# only substitution is the engine ref via the __REF__ sentinel.
_WORKFLOW_TEMPLATE = """name: AI Peer Review
on:
  pull_request:
    types: [opened, synchronize, reopened]
  issue_comment:
    types: [created]
  check_run:
    types: [completed]
  schedule:
    - cron: "*/5 * * * *"
permissions:
  contents: read
  pull-requests: write
  issues: write
  actions: read
  checks: read
concurrency:
  group: ai-peer-review-${{ github.event.pull_request.number || github.event.issue.number || github.run_id }}
  cancel-in-progress: false
jobs:
  review:
    runs-on: ubuntu-latest
    if: |
      github.event_name == 'pull_request' ||
      github.event_name == 'check_run' ||
      github.event_name == 'schedule' ||
      (github.event_name == 'issue_comment' && github.event.issue.pull_request != null)
    steps:
      - name: Checkout base branch (never PR head)
        uses: actions/checkout@b4ffde65f46336ab88eb53be808477a3936bae11  # v4.1.1
        with:
          ref: ${{ github.event.repository.default_branch }}
      - name: Run AI peer review
        uses: NFS-247/ai-peer-review@__REF__
        with:
          github_token: ${{ secrets.GITHUB_TOKEN }}
          anthropic_api_key: ${{ secrets.ANTHROPIC_API_KEY }}
          openai_api_key: ${{ secrets.OPENAI_API_KEY }}
          gemini_api_key: ${{ secrets.GEMINI_API_KEY }}
          xai_api_key: ${{ secrets.XAI_API_KEY }}
          dispatcher_verdict_secret: ${{ secrets.DISPATCHER_VERDICT_SECRET }}
"""


class ProvisioningClient(Protocol):
    """The GitHub operations the provisioner needs, behind an interface.

    Implemented for real against a GitHub-App installation token (Administration
    /Contents/Secrets/Workflows: write). A test/fake records the calls.
    """

    def repo_exists(self, owner: str, repo: str) -> bool: ...
    def create_repo(self, owner: str, repo: str, *, private: bool = True) -> None: ...
    def put_file(self, owner: str, repo: str, path: str, content: str, message: str) -> None: ...
    def set_secret(self, owner: str, repo: str, name: str, value: str) -> None: ...


@dataclass(frozen=True)
class ProvisionResult:
    owner: str
    repo: str
    created: bool             # True if we created the repo (vs wired an existing one)
    secrets_set: tuple        # names of the Actions secrets written


def gen_verdict_secret() -> str:
    """A fresh per-repo HMAC secret — the cross-tenant verdict-forgery boundary.

    Each tenant signs with its own, so a leak in one repo can't forge verdicts in
    another. 32 random bytes as hex (256-bit).
    """
    return _secrets.token_hex(32)


def caller_workflow_yaml(ref: str = ENGINE_REF) -> str:
    """The single workflow file a consuming repo needs, pinned to ``ref`` (@v3)."""
    return _WORKFLOW_TEMPLATE.replace("__REF__", ref)


def peer_review_config_json(operator_login: str) -> str:
    """Minimal .peer-review.json: the operator is the connecting user, so the
    engine honors THEIR approve/block on this repo (and only theirs)."""
    return json.dumps({"operator_github_login": operator_login}, indent=2) + "\n"


def provision(
    client: ProvisioningClient,
    *,
    owner: str,
    repo: str,
    operator_login: str,
    api_keys: Mapping[str, str],
    verdict_secret: Optional[str] = None,
    private: bool = True,
) -> ProvisionResult:
    """Make ``owner/repo`` review-ready. Creates the repo if it doesn't exist,
    commits the workflow + config, and sets the Actions secrets.

    ``api_keys`` maps secret name -> value (e.g. ANTHROPIC_API_KEY); only non-empty
    supplied keys are set, but ANTHROPIC_API_KEY + OPENAI_API_KEY are required (the
    default panel) and a missing one raises ValueError before anything is written.
    """
    if not owner or not repo:
        raise ValueError("owner and repo are required")
    if not operator_login:
        raise ValueError("operator_login is required (the engine gates writes on it)")
    keys = {k: (api_keys.get(k) or "").strip() for k in MODEL_KEY_SECRETS}
    missing = [k for k in REQUIRED_KEY_SECRETS if not keys[k]]
    if missing:
        raise ValueError(f"missing required model key(s): {', '.join(missing)}")

    created = False
    if not client.repo_exists(owner, repo):
        client.create_repo(owner, repo, private=private)
        created = True

    client.put_file(owner, repo, WORKFLOW_PATH, caller_workflow_yaml(),
                    "Add AI peer-review workflow")
    client.put_file(owner, repo, CONFIG_PATH, peer_review_config_json(operator_login),
                    "Add AI peer-review config")

    set_names: list[str] = []
    client.set_secret(owner, repo, "DISPATCHER_VERDICT_SECRET",
                      verdict_secret or gen_verdict_secret())
    set_names.append("DISPATCHER_VERDICT_SECRET")
    for name in MODEL_KEY_SECRETS:
        if keys[name]:
            client.set_secret(owner, repo, name, keys[name])
            set_names.append(name)

    return ProvisionResult(owner=owner, repo=repo, created=created,
                           secrets_set=tuple(set_names))


__all__ = [
    "ProvisioningClient", "ProvisionResult", "provision",
    "gen_verdict_secret", "caller_workflow_yaml", "peer_review_config_json",
    "WORKFLOW_PATH", "CONFIG_PATH", "ENGINE_REF",
]
