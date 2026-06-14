"""Tests for the self-serve provisioning orchestration (front_door.app.provision).

Pure orchestration against a fake client — no GitHub. The provider SELECTION (the
UI checkboxes) drives both the Actions secrets AND the tier rosters, so the panel
only uses models the repo has keys for; secrets are written before the workflow.
"""

import json

import pytest

from front_door.app import provision as P


class FakeClient:
    def __init__(self, existing=()):
        self._existing = set(existing)
        self.created: list = []
        self.files: dict = {}     # (owner, repo, path) -> content
        self.secrets: dict = {}   # (owner, repo, name) -> value
        self.ops: list = []       # ordered ("create"|"secret"|"file", detail)

    def repo_exists(self, owner, repo):
        return (owner, repo) in self._existing

    def create_repo(self, owner, repo, *, private=True):
        self.ops.append(("create", repo))
        self.created.append((owner, repo, private))
        self._existing.add((owner, repo))

    def put_file(self, owner, repo, path, content, message):
        self.ops.append(("file", path))
        self.files[(owner, repo, path)] = content

    def set_secret(self, owner, repo, name, value):
        self.ops.append(("secret", name))
        self.secrets[(owner, repo, name)] = value


def test_provision_creates_repo_and_wires_all_selected_providers():
    c = FakeClient()
    res = P.provision(c, owner="alice", repo="idea", operator_login="alice",
                      api_keys={"anthropic": "sk-ant-x", "openai": "sk-oai-x",
                                "gemini": "g", "xai": "xai-z"})
    assert res.created is True
    assert c.created == [("alice", "idea", True)]          # private by default
    assert res.reviewers == ("claude", "gpt", "gemini", "grok")  # canonical order
    wf = c.files[("alice", "idea", P.WORKFLOW_PATH)]
    cfg = json.loads(c.files[("alice", "idea", P.CONFIG_PATH)])
    assert "NFS-247/ai-peer-review@v3" in wf
    assert cfg["operator_github_login"] == "alice"
    assert cfg["high_stakes_reviewers"] == ["claude", "gpt", "gemini", "grok"]
    assert cfg["routine_reviewers"] == ["claude", "gpt", "gemini", "grok"]
    assert c.secrets[("alice", "idea", "XAI_API_KEY")] == "xai-z"
    assert set(res.secrets_set) == {
        "DISPATCHER_VERDICT_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "GEMINI_API_KEY", "XAI_API_KEY",
    }


def test_provision_subset_drives_roster_and_only_those_secrets():
    # The checkbox feature: pick Anthropic + Gemini only -> panel is claude+gemini,
    # only those two keys are set, and the rosters match so the engine never
    # requires the unpicked gpt/grok (which would stall every PR).
    c = FakeClient()
    res = P.provision(c, owner="a", repo="r", operator_login="a",
                      api_keys={"anthropic": "x", "gemini": "y"})
    assert res.reviewers == ("claude", "gemini")
    cfg = json.loads(c.files[("a", "r", P.CONFIG_PATH)])
    assert cfg["high_stakes_reviewers"] == ["claude", "gemini"]
    assert cfg["backend_reviewers"] == ["claude", "gemini"]
    assert ("a", "r", "OPENAI_API_KEY") not in c.secrets
    assert ("a", "r", "XAI_API_KEY") not in c.secrets
    assert c.secrets[("a", "r", "ANTHROPIC_API_KEY")] == "x"
    assert c.secrets[("a", "r", "GEMINI_API_KEY")] == "y"


def test_provision_secrets_written_before_workflow():
    # A repo with existing PRs must not see the workflow before its keys exist.
    c = FakeClient()
    P.provision(c, owner="a", repo="r", operator_login="a",
                api_keys={"anthropic": "x", "openai": "y"})
    order = [kind for kind, _ in c.ops if kind in ("secret", "file")]
    last_secret = max(i for i, k in enumerate(order) if k == "secret")
    first_file = min(i for i, k in enumerate(order) if k == "file")
    assert last_secret < first_file


def test_provision_wires_existing_repo_without_creating():
    c = FakeClient(existing={("alice", "idea")})
    res = P.provision(c, owner="alice", repo="idea", operator_login="alice",
                      api_keys={"anthropic": "x", "openai": "y"})
    assert res.created is False
    assert c.created == []
    assert ("alice", "idea", P.WORKFLOW_PATH) in c.files


def test_provision_generates_unique_verdict_secret_when_not_given():
    c1, c2 = FakeClient(), FakeClient()
    kw = dict(operator_login="a", api_keys={"anthropic": "x", "openai": "y"})
    P.provision(c1, owner="a", repo="r", **kw)
    P.provision(c2, owner="a", repo="r", **kw)
    s1 = c1.secrets[("a", "r", "DISPATCHER_VERDICT_SECRET")]
    s2 = c2.secrets[("a", "r", "DISPATCHER_VERDICT_SECRET")]
    assert len(s1) == 64 and len(s2) == 64 and s1 != s2


def test_provision_uses_given_verdict_secret():
    c = FakeClient()
    P.provision(c, owner="a", repo="r", operator_login="a",
                api_keys={"anthropic": "x", "openai": "y"}, verdict_secret="fixed")
    assert c.secrets[("a", "r", "DISPATCHER_VERDICT_SECRET")] == "fixed"


def test_provision_requires_at_least_one_provider():
    c = FakeClient()
    with pytest.raises(ValueError):
        P.provision(c, owner="a", repo="r", operator_login="a", api_keys={})
    assert c.files == {} and c.secrets == {}   # nothing written on failure


def test_provision_single_provider_allowed():
    # 1 is allowed ("whatever you want") — it's just a 1-model panel.
    c = FakeClient()
    res = P.provision(c, owner="a", repo="r", operator_login="a",
                      api_keys={"anthropic": "x"})
    assert res.reviewers == ("claude",)
    assert json.loads(c.files[("a", "r", P.CONFIG_PATH)])["high_stakes_reviewers"] == ["claude"]


def test_provision_requires_operator_login():
    c = FakeClient()
    with pytest.raises(ValueError):
        P.provision(c, owner="a", repo="r", operator_login="",
                    api_keys={"anthropic": "x", "openai": "y"})


def test_caller_workflow_pins_ref_and_has_no_unsubstituted_sentinel():
    wf = P.caller_workflow_yaml()
    assert "NFS-247/ai-peer-review@v3" in wf
    assert "__REF__" not in wf
    assert "schedule:" in wf and "*/5 * * * *" in wf
