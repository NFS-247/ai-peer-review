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
from typing import Mapping, Optional, Protocol, Sequence


WORKFLOW_PATH = ".github/workflows/ai-peer-review.yml"
CONFIG_PATH = ".peer-review.json"
ENGINE_REF = "v3"

# Each provider the panel can use: checkbox id -> (Actions secret name, reviewer
# name in a tier roster). The UI shows one checkbox per provider; the user picks
# which to use (BYOK — they pay each provider directly). ONLY the selected ones
# get a secret AND appear in the rosters, so the engine never requires a model the
# repo has no key for.
PROVIDERS: dict = {
    "anthropic": ("ANTHROPIC_API_KEY", "claude"),
    "openai": ("OPENAI_API_KEY", "gpt"),
    "gemini": ("GEMINI_API_KEY", "gemini"),
    "xai": ("XAI_API_KEY", "grok"),
}
# Canonical roster order, independent of the order the keys arrive in.
PROVIDER_ORDER = ("anthropic", "openai", "gemini", "xai")

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
    reviewers: tuple          # the panel chosen, e.g. ("claude", "gemini")
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


def peer_review_config_json(operator_login: str, reviewers: Sequence[str]) -> str:
    """The repo's .peer-review.json: the operator is the connecting user (the
    engine honors THEIR approve/block, and only theirs), and every tier's roster
    is exactly the providers they selected — so the panel only ever uses models
    the repo actually has keys for."""
    roster = list(reviewers)
    return json.dumps({
        "operator_github_login": operator_login,
        "routine_reviewers": roster,
        "backend_reviewers": roster,
        "high_stakes_reviewers": roster,
    }, indent=2) + "\n"


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
    """Make ``owner/repo`` review-ready from the user's provider selection.

    ``api_keys`` maps PROVIDER id (see PROVIDERS: anthropic/openai/gemini/xai) ->
    that provider's key. The SELECTED providers are those with a non-empty key:
    only they get an Actions secret AND appear in the tier rosters, so the engine
    never requires a model the repo has no key for. At least one provider is
    required (two or more for genuinely adversarial review).

    The workflow is the activation switch, so it is committed LAST — after both
    the secrets and the roster config — so wiring a repo that already has open PRs
    can't trigger a run that reads default rosters or runs key-less.
    """
    if not owner or not repo:
        raise ValueError("owner and repo are required")
    if not operator_login:
        raise ValueError("operator_login is required (the engine gates writes on it)")
    selected = [p for p in PROVIDER_ORDER if (api_keys.get(p) or "").strip()]
    if not selected:
        raise ValueError("select at least one provider with an API key")
    reviewers = tuple(PROVIDERS[p][1] for p in selected)

    created = False
    if not client.repo_exists(owner, repo):
        client.create_repo(owner, repo, private=private)
        created = True

    # Secrets FIRST — a repo with existing PRs must never see the workflow before
    # its keys exist (that would run the panel key-less and escalate).
    set_names: list[str] = ["DISPATCHER_VERDICT_SECRET"]
    client.set_secret(owner, repo, "DISPATCHER_VERDICT_SECRET",
                      verdict_secret or gen_verdict_secret())
    for p in selected:
        secret_name = PROVIDERS[p][0]
        client.set_secret(owner, repo, secret_name, api_keys[p].strip())
        set_names.append(secret_name)

    # Config BEFORE workflow, workflow LAST. The workflow is the activation
    # switch — it carries the schedule cron and the pull_request triggers — so it
    # must be the final write, after BOTH the secrets and the roster config are in
    # place. If it landed first, a scheduled/PR-triggered run could fire reading
    # the default rosters (which require gpt) before the subset override exists,
    # recreating the very "required reviewer unavailable" stall this prevents.
    client.put_file(owner, repo, CONFIG_PATH,
                    peer_review_config_json(operator_login, reviewers),
                    "Add AI peer-review config")
    client.put_file(owner, repo, WORKFLOW_PATH, caller_workflow_yaml(),
                    "Add AI peer-review workflow")

    return ProvisionResult(owner=owner, repo=repo, created=created,
                           reviewers=reviewers, secrets_set=tuple(set_names))


__all__ = [
    "ProvisioningClient", "ProvisionResult", "provision",
    "gen_verdict_secret", "caller_workflow_yaml", "peer_review_config_json",
    "PROVIDERS", "PROVIDER_ORDER", "WORKFLOW_PATH", "CONFIG_PATH", "ENGINE_REF",
]
