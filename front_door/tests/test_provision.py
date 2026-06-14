"""Tests for the self-serve provisioning orchestration (front_door.app.provision).

Pure orchestration against a fake client — no GitHub. Proves a connect wires a
repo review-ready: the @v3 workflow + operator config committed, and the BYOK
keys + a generated verdict secret set, with the required-keys guard up front.
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

    def repo_exists(self, owner, repo):
        return (owner, repo) in self._existing

    def create_repo(self, owner, repo, *, private=True):
        self.created.append((owner, repo, private))
        self._existing.add((owner, repo))

    def put_file(self, owner, repo, path, content, message):
        self.files[(owner, repo, path)] = content

    def set_secret(self, owner, repo, name, value):
        self.secrets[(owner, repo, name)] = value


_KEYS = {"ANTHROPIC_API_KEY": "sk-ant-x", "OPENAI_API_KEY": "sk-oai-x"}


def test_provision_creates_repo_when_missing_and_wires_everything():
    c = FakeClient()
    res = P.provision(c, owner="alice", repo="idea", operator_login="alice",
                      api_keys={**_KEYS, "GEMINI_API_KEY": "g", "XAI_API_KEY": "xai-z"})
    assert res.created is True
    assert c.created == [("alice", "idea", True)]   # private by default
    # both files committed
    wf = c.files[("alice", "idea", P.WORKFLOW_PATH)]
    cfg = c.files[("alice", "idea", P.CONFIG_PATH)]
    assert "NFS-247/ai-peer-review@v3" in wf            # pinned to the current engine
    assert "${{ secrets.XAI_API_KEY }}" in wf          # forwards the key
    assert json.loads(cfg)["operator_github_login"] == "alice"
    # secrets: verdict secret + every supplied key
    assert c.secrets[("alice", "idea", "DISPATCHER_VERDICT_SECRET")]
    assert c.secrets[("alice", "idea", "ANTHROPIC_API_KEY")] == "sk-ant-x"
    assert c.secrets[("alice", "idea", "XAI_API_KEY")] == "xai-z"
    assert set(res.secrets_set) == {
        "DISPATCHER_VERDICT_SECRET", "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "GEMINI_API_KEY", "XAI_API_KEY",
    }


def test_provision_wires_existing_repo_without_creating():
    c = FakeClient(existing={("alice", "idea")})
    res = P.provision(c, owner="alice", repo="idea", operator_login="alice", api_keys=_KEYS)
    assert res.created is False
    assert c.created == []                              # did not re-create
    assert (("alice", "idea", P.WORKFLOW_PATH)) in c.files


def test_provision_sets_only_supplied_keys():
    c = FakeClient()
    P.provision(c, owner="a", repo="r", operator_login="a", api_keys=_KEYS)  # no gemini/xai
    assert ("a", "r", "GEMINI_API_KEY") not in c.secrets
    assert ("a", "r", "XAI_API_KEY") not in c.secrets
    assert ("a", "r", "OPENAI_API_KEY") in c.secrets


def test_provision_generates_unique_verdict_secret_when_not_given():
    c1, c2 = FakeClient(), FakeClient()
    P.provision(c1, owner="a", repo="r", operator_login="a", api_keys=_KEYS)
    P.provision(c2, owner="a", repo="r", operator_login="a", api_keys=_KEYS)
    s1 = c1.secrets[("a", "r", "DISPATCHER_VERDICT_SECRET")]
    s2 = c2.secrets[("a", "r", "DISPATCHER_VERDICT_SECRET")]
    assert len(s1) == 64 and len(s2) == 64 and s1 != s2   # 256-bit hex, per-repo


def test_provision_uses_given_verdict_secret():
    c = FakeClient()
    P.provision(c, owner="a", repo="r", operator_login="a", api_keys=_KEYS,
                verdict_secret="fixed-secret")
    assert c.secrets[("a", "r", "DISPATCHER_VERDICT_SECRET")] == "fixed-secret"


def test_provision_requires_default_panel_keys():
    c = FakeClient()
    with pytest.raises(ValueError) as e:
        P.provision(c, owner="a", repo="r", operator_login="a",
                    api_keys={"ANTHROPIC_API_KEY": "x"})   # missing OPENAI
    assert "OPENAI_API_KEY" in str(e.value)
    assert c.files == {} and c.secrets == {}               # nothing written on failure


def test_provision_requires_operator_login():
    c = FakeClient()
    with pytest.raises(ValueError):
        P.provision(c, owner="a", repo="r", operator_login="", api_keys=_KEYS)


def test_caller_workflow_pins_ref_and_has_no_unsubstituted_sentinel():
    wf = P.caller_workflow_yaml()
    assert "NFS-247/ai-peer-review@v3" in wf
    assert "__REF__" not in wf
    assert "schedule:" in wf and "*/5 * * * *" in wf       # the sweep is wired in
