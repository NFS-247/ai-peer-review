"""GitHubProvisioner (the real ProvisioningClient) — request shaping + sealing.

The HTTP is faked by recording calls on a stub ``_request`` (the seam gh.py
documents). The libsodium sealed-box round-trip uses real PyNaCl and skips where
it isn't installed, so this suite stays green in a stdlib-only environment while
still proving the crypto where the dep is present.
"""

import base64

import pytest

from front_door.app import provision_client as PC
from front_door.app.gh import GitHubError


class FakeGH:
    def __init__(self):
        self.calls: list = []
        self.responses: dict = {}   # (method, path) -> value | Exception

    def _request(self, method, path, body=None):
        self.calls.append((method, path, body))
        r = self.responses.get((method, path))
        if isinstance(r, Exception):
            raise r
        return r

    def last(self, method, path):
        for m, p, b in reversed(self.calls):
            if m == method and p == path:
                return b
        return None


def _prov(gh, login="alice"):
    return PC.GitHubProvisioner(gh, authed_login=login)


# ---- repo_exists ------------------------------------------------------------
def test_repo_exists_true_on_200():
    gh = FakeGH()
    gh.responses[("GET", "/repos/alice/r")] = {"id": 1}
    assert _prov(gh).repo_exists("alice", "r") is True


def test_repo_exists_false_on_404_but_reraises_others():
    gh = FakeGH()
    gh.responses[("GET", "/repos/alice/r")] = GitHubError("not found", status=404)
    assert _prov(gh).repo_exists("alice", "r") is False

    # A 403 (no access) must NOT be read as "doesn't exist" -> would try create_repo.
    gh.responses[("GET", "/repos/alice/r")] = GitHubError("forbidden", status=403)
    with pytest.raises(GitHubError):
        _prov(gh).repo_exists("alice", "r")


# ---- create_repo ------------------------------------------------------------
def test_create_repo_under_own_account():
    gh = FakeGH()
    _prov(gh, login="alice").create_repo("alice", "r", private=True)
    body = gh.last("POST", "/user/repos")
    assert body == {"name": "r", "private": True, "auto_init": True}


def test_create_repo_under_org_uses_org_endpoint():
    gh = FakeGH()
    _prov(gh, login="alice").create_repo("acme", "r", private=False)
    assert gh.last("POST", "/orgs/acme/repos") == {"name": "r", "private": False, "auto_init": True}
    assert gh.last("POST", "/user/repos") is None


# ---- put_file ---------------------------------------------------------------
def test_put_file_new_file_has_no_sha_and_base64_content():
    gh = FakeGH()
    gh.responses[("GET", "/repos/alice/r/contents/x.txt")] = GitHubError("nope", status=404)
    _prov(gh).put_file("alice", "r", "x.txt", "hello", "msg")
    body = gh.last("PUT", "/repos/alice/r/contents/x.txt")
    assert "sha" not in body
    assert base64.b64decode(body["content"]).decode() == "hello"
    assert body["message"] == "msg"


def test_put_file_existing_file_includes_blob_sha():
    gh = FakeGH()
    gh.responses[("GET", "/repos/alice/r/contents/x.txt")] = {"sha": "deadbeef"}
    _prov(gh).put_file("alice", "r", "x.txt", "hi", "msg")
    assert gh.last("PUT", "/repos/alice/r/contents/x.txt")["sha"] == "deadbeef"


# ---- set_secret (seal stubbed so this needs no crypto dep) ------------------
def test_set_secret_fetches_key_seals_and_puts(monkeypatch):
    monkeypatch.setattr(PC, "seal_secret", lambda pk, v: f"SEALED({pk}:{v})")
    gh = FakeGH()
    gh.responses[("GET", "/repos/alice/r/actions/secrets/public-key")] = {
        "key_id": "kid7", "key": "PUBKEY"}
    _prov(gh).set_secret("alice", "r", "ANTHROPIC_API_KEY", "sk-ant")
    body = gh.last("PUT", "/repos/alice/r/actions/secrets/ANTHROPIC_API_KEY")
    assert body == {"encrypted_value": "SEALED(PUBKEY:sk-ant)", "key_id": "kid7"}


def test_set_secret_raises_when_public_key_missing(monkeypatch):
    # An empty/malformed public-key response must fail loudly, not seal to ''.
    monkeypatch.setattr(PC, "seal_secret", lambda pk, v: "x")
    gh = FakeGH()
    gh.responses[("GET", "/repos/alice/r/actions/secrets/public-key")] = {}
    with pytest.raises(GitHubError):
        _prov(gh).set_secret("alice", "r", "NAME", "val")
    assert gh.last("PUT", "/repos/alice/r/actions/secrets/NAME") is None  # nothing PUT


def test_seal_secret_without_nacl_raises_actionable_error(monkeypatch):
    # Simulate the dep being absent: force the lazy import to fail.
    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name.startswith("nacl"):
            raise ImportError("no nacl")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(GitHubError) as ei:
        PC.seal_secret("cHVia2V5", "x")
    assert "pynacl" in str(ei.value).lower()


# ---- real sealed box (skips without PyNaCl) ---------------------------------
def test_seal_secret_roundtrips_with_real_nacl():
    pytest.importorskip("nacl")
    from nacl.encoding import Base64Encoder
    from nacl.public import PrivateKey, SealedBox

    sk = PrivateKey.generate()
    pk_b64 = sk.public_key.encode(Base64Encoder).decode()
    ciphertext = PC.seal_secret(pk_b64, "s3cr3t-value")
    plaintext = SealedBox(sk).decrypt(base64.b64decode(ciphertext))
    assert plaintext == b"s3cr3t-value"
