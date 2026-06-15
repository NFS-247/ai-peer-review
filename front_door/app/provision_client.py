"""Real ``ProvisioningClient`` over the GitHub REST API (see provision.py).

This is the concrete adapter the self-serve Connect flow uses. It deliberately
runs on the **operator's own OAuth token** (the same ``repo``-scoped token that
already powers approvals) rather than a separate GitHub App: a user wires THEIR
OWN repo with the sign-in they already did, so there is nothing extra to register
or install. The token's ``repo`` scope covers everything provisioning needs —
create the repo, commit the workflow + config, and set the Actions secrets.

Secret values must be encrypted to the repo's public key with a libsodium sealed
box before the API will accept them (GitHub never receives the plaintext). That
is the one piece needing a crypto lib (PyNaCl); it is imported lazily inside
``seal_secret`` so importing this module — and running the rest of the front_door
test suite — never requires PyNaCl. The deployment installs it (see
requirements.txt); ``seal_secret`` raises a clear error if it is missing.
"""

from __future__ import annotations

import base64

from .gh import GitHub, GitHubError


def seal_secret(public_key_b64: str, value: str) -> str:
    """Encrypt ``value`` to a repo's Actions public key (libsodium sealed box).

    Returns the base64 ciphertext GitHub's "create or update a secret" endpoint
    expects. PyNaCl is imported here, not at module load, so the dependency is
    needed only when a secret is actually set — raising a clear, actionable error
    if the deployment forgot to install it.
    """
    try:
        from nacl import encoding, public
    except ImportError as exc:  # deployment didn't install the crypto dep
        raise GitHubError(
            "setting Actions secrets requires PyNaCl for libsodium sealed-box "
            "encryption (pip install pynacl); it is not installed in this "
            "environment."
        ) from exc
    pk = public.PublicKey(public_key_b64.encode("utf-8"), encoding.Base64Encoder)
    sealed = public.SealedBox(pk).encrypt(value.encode("utf-8"))
    return base64.b64encode(sealed).decode("utf-8")


class GitHubProvisioner:
    """Implements provision.py's ``ProvisioningClient`` against the GitHub API.

    Wraps a ``GitHub`` client (carrying the operator's token); ``authed_login`` is
    that token's own login, used only to decide whether a new repo is created
    under the user's account (``POST /user/repos``) or an org they admin
    (``POST /orgs/{owner}/repos``).
    """

    def __init__(self, gh: GitHub, *, authed_login: str) -> None:
        self._gh = gh
        self._login = authed_login

    @property
    def login(self) -> str:
        """The token's GitHub login — the repo owner + operator for provisioning."""
        return self._login

    # ---- ProvisioningClient ------------------------------------------------
    def repo_exists(self, owner: str, repo: str) -> bool:
        try:
            self._gh._request("GET", f"/repos/{owner}/{repo}")
            return True
        except GitHubError as exc:
            if " 404" in str(exc):       # not found vs a real error (auth, rate)
                return False
            raise

    def create_repo(self, owner: str, repo: str, *, private: bool = True) -> None:
        # auto_init=True gives the repo an initial commit + default branch, so the
        # contents API can add the workflow/config as ordinary updates rather than
        # having to bootstrap the first commit on an empty repo.
        body = {"name": repo, "private": private, "auto_init": True}
        if owner and owner != self._login:
            self._gh._request("POST", f"/orgs/{owner}/repos", body)
        else:
            self._gh._request("POST", "/user/repos", body)

    def put_file(self, owner: str, repo: str, path: str, content: str, message: str) -> None:
        body = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("utf-8"),
        }
        sha = self._file_sha(owner, repo, path)
        if sha:                          # updating an existing file needs its blob sha
            body["sha"] = sha
        self._gh._request("PUT", f"/repos/{owner}/{repo}/contents/{path}", body)

    def set_secret(self, owner: str, repo: str, name: str, value: str) -> None:
        key = self._gh._request(
            "GET", f"/repos/{owner}/{repo}/actions/secrets/public-key") or {}
        encrypted = seal_secret(key.get("key", ""), value)
        self._gh._request(
            "PUT", f"/repos/{owner}/{repo}/actions/secrets/{name}",
            {"encrypted_value": encrypted, "key_id": key.get("key_id", "")})

    # ---- helpers -----------------------------------------------------------
    def _file_sha(self, owner: str, repo: str, path: str) -> str:
        """Blob sha of an existing file, or '' if it doesn't exist yet."""
        try:
            data = self._gh._request("GET", f"/repos/{owner}/{repo}/contents/{path}")
        except GitHubError as exc:
            if " 404" in str(exc):
                return ""
            raise
        return (data or {}).get("sha", "") if isinstance(data, dict) else ""


__all__ = ["GitHubProvisioner", "seal_secret"]
