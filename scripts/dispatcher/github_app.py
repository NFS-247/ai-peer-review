"""Pure-stdlib GitHub App authentication: mint per-installation access tokens.

Why this exists: every GitHub call the platform makes through ONE shared identity
(a PAT, or the operator's OAuth token) competes for a single 5,000 req/hr bucket —
so more tenants and busier days hit the wall (see SCALING.md, Move 1). A GitHub
App instead gets a SEPARATE 5,000–15,000 req/hr quota *per installation*, so
capacity grows with tenants and a noisy tenant can't starve the others.

Minting an installation token is a two-step dance the App must do itself:
  1. sign a short-lived JWT with the App's RSA private key (RS256) — "I am this
     App";
  2. exchange it at ``POST /app/installations/{id}/access_tokens`` for a 1-hour
     token scoped to that installation, which the REST client then uses.

Step 1 needs RS256 (RSASSA-PKCS1-v1_5 over SHA-256). The engine is stdlib-only
(enforced by tests/test_dispatcher_standalone.py), so there is no PyJWT /
cryptography to lean on — the signing is implemented here against the stdlib:
parse the PEM key's DER for (modulus, private exponent), PKCS#1 v1.5-pad the
SHA-256 DigestInfo, and RSA-sign with the big-integer modexp built into ``pow``.
A bug here fails *closed* (GitHub rejects the JWT, no token is issued), never open.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable, Optional


GITHUB_API = "https://api.github.com"

# DER-encoded DigestInfo prefix for SHA-256 (RFC 8017 §9.2): the AlgorithmIdentifier
# + OCTET STRING header that precedes the 32-byte digest in a PKCS#1 v1.5 signature.
_SHA256_DIGESTINFO_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")

# An installation token lasts 1h; refresh at 50m so an in-flight run never carries
# an about-to-expire token. The JWT is backdated 60s (clock drift) and lives 8m,
# inside GitHub's 10-minute ceiling — it is used immediately, only to mint.
_TOKEN_TTL_SECONDS = 3000
_TOKEN_EXPIRY_SKEW = 600  # refresh this long before the API-reported expiry
_JWT_BACKDATE_SECONDS = 60
_JWT_LIFETIME_SECONDS = 480


def _b64url(data: bytes) -> str:
    """Base64url without padding (the JWT/JWS wire format)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _compact_json(obj: dict) -> bytes:
    """Deterministic, minimal JSON for a JWT segment."""
    return json.dumps(obj, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _parse_expires_at(value: Optional[str]) -> Optional[float]:
    """Epoch seconds for GitHub's ISO-8601 ``expires_at``, or None if unparseable."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None


def _der_read_tlv(data: bytes, i: int) -> tuple[int, bytes, int]:
    """Read one DER TLV at ``data[i]``; return (tag, value_bytes, next_index).

    Every read is bounds-checked, so truncated or garbage input raises a clear
    ValueError rather than an IndexError escaping the parser.
    """
    if i + 1 >= len(data):
        raise ValueError("truncated DER: missing tag/length")
    tag = data[i]
    length = data[i + 1]
    i += 2
    if length & 0x80:  # long form: low 7 bits = number of length bytes
        num = length & 0x7F
        if num == 0:
            # 0x80 = indefinite length: forbidden in DER, and would otherwise
            # misparse as a zero-length value. Reject rather than silently corrupt.
            raise ValueError("indefinite-length DER is not supported")
        if i + num > len(data):
            raise ValueError("truncated DER: length bytes")
        length = int.from_bytes(data[i:i + num], "big")
        i += num
    if i + length > len(data):
        raise ValueError("truncated DER: value")
    return tag, data[i:i + length], i + length


def _parse_rsa_private_key(pem: str) -> tuple[int, int]:
    """Extract (modulus n, private exponent d) from a PEM RSA private key.

    Accepts PKCS#1 (``BEGIN RSA PRIVATE KEY``) and unencrypted PKCS#8
    (``BEGIN PRIVATE KEY``) — the two forms a GitHub App key is distributed or
    converted to. Encrypted keys are rejected with a clear message.
    """
    text = (pem or "").strip()
    if "ENCRYPTED" in text:
        raise ValueError(
            "encrypted private keys are not supported; supply the unencrypted "
            "GitHub App private key"
        )
    if "PRIVATE KEY" not in text:
        raise ValueError("not a PEM-encoded private key")
    body = "".join(
        s
        for s in (ln.strip() for ln in text.splitlines())
        if s and not s.startswith("-----")  # filter on the STRIPPED line
    )
    try:
        der = base64.b64decode(body)
    except Exception as exc:  # noqa: BLE001 - normalize to a clear error
        raise ValueError(f"invalid PEM base64: {exc}") from exc

    try:
        if "BEGIN PRIVATE KEY" in text:  # PKCS#8: unwrap to the RSAPrivateKey
            _, info, _ = _der_read_tlv(der, 0)      # PrivateKeyInfo SEQUENCE
            j = 0
            _, _ver, j = _der_read_tlv(info, j)     # version
            _, _alg, j = _der_read_tlv(info, j)     # AlgorithmIdentifier
            _, der, j = _der_read_tlv(info, j)      # privateKey OCTET STRING -> DER

        tag, seq, _ = _der_read_tlv(der, 0)         # RSAPrivateKey SEQUENCE
        if tag != 0x30:
            raise ValueError("expected a SEQUENCE")
        j = 0
        _, _v, j = _der_read_tlv(seq, j)            # version
        _, n_bytes, j = _der_read_tlv(seq, j)       # modulus
        _, _e, j = _der_read_tlv(seq, j)            # publicExponent
        _, d_bytes, j = _der_read_tlv(seq, j)       # privateExponent
        return int.from_bytes(n_bytes, "big"), int.from_bytes(d_bytes, "big")
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - truncated/garbage DER -> clear error
        raise ValueError(f"malformed RSA private key: {exc}") from exc


def _sign_rs256(message: bytes, n: int, d: int) -> bytes:
    """RSASSA-PKCS1-v1_5 signature over SHA-256(message) — JWS 'RS256'."""
    digest_info = _SHA256_DIGESTINFO_PREFIX + hashlib.sha256(message).digest()
    k = (n.bit_length() + 7) // 8                   # modulus size in bytes
    pad_len = k - len(digest_info) - 3
    if pad_len < 8:
        raise ValueError("RSA key too small for an RS256 signature")
    # EM = 0x00 || 0x01 || 0xFF*pad_len || 0x00 || DigestInfo   (RFC 8017 §9.2)
    em = b"\x00\x01" + (b"\xff" * pad_len) + b"\x00" + digest_info
    signature = pow(int.from_bytes(em, "big"), d, n)
    return signature.to_bytes(k, "big")


def _sign_jwt(app_id: object, n: int, d: int, *, now: Optional[float] = None) -> str:
    """Build a signed App JWT from already-parsed key components (n, d).

    ``iat`` is backdated 60s for clock drift and ``exp`` is 8 min out — inside
    GitHub's 10-minute ceiling. ``iss`` is the App ID (or client ID).
    """
    issued = int(now if now is not None else time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": issued - _JWT_BACKDATE_SECONDS,
        "exp": issued + _JWT_LIFETIME_SECONDS,
        "iss": str(app_id),
    }
    signing_input = f"{_b64url(_compact_json(header))}.{_b64url(_compact_json(payload))}"
    signature = _sign_rs256(signing_input.encode("ascii"), n, d)
    return f"{signing_input}.{_b64url(signature)}"


def build_app_jwt(
    app_id: object,
    private_key_pem: str,
    *,
    now: Optional[float] = None,
) -> str:
    """Parse ``private_key_pem`` and build a signed App JWT (RS256)."""
    n, d = _parse_rsa_private_key(private_key_pem)
    return _sign_jwt(app_id, n, d, now=now)


class GitHubAppError(RuntimeError):
    """A failure minting a GitHub App installation token.

    ``status`` is the HTTP status when the failure came from the API (404 = the
    App isn't installed on the repo, 401 = bad credentials, 5xx = a GitHub-side
    fault); it is None for a transport error (DNS / connection). Callers use it to
    tell a *misconfig* (4xx) from a *transient* fault (5xx / network).
    """

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


def _api_request(
    method: str,
    url: str,
    *,
    jwt: str,
    body: Optional[dict] = None,
    timeout: int = 30,
) -> dict:
    """One authenticated GitHub API call with the App JWT (stdlib urllib)."""
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {jwt}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "TradeWatcher-Dispatcher/1.0",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Surface a useful status + body, but raise our own error so the request
        # (with its Authorization: Bearer <JWT> header) can't ride into a CI log.
        detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        raise GitHubAppError(
            f"GitHub App API {method} {url} failed: HTTP {exc.code}: {detail[:300]}",
            status=exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        # Transport-level failure (DNS / connection / timeout): no HTTP status.
        raise GitHubAppError(
            f"GitHub App API {method} {url} failed: {exc.reason}", status=None
        ) from exc


class GitHubApp:
    """Mints per-installation access tokens for a GitHub App (stdlib only).

    Tokens are cached per installation and refreshed ~10 min before the 1-hour
    expiry, so a long-lived caller (the front door) mints rarely. Installation
    ids are resolved once per (owner, repo) and cached.
    """

    def __init__(
        self,
        app_id: object,
        private_key_pem: str,
        *,
        api_url: str = GITHUB_API,
        now: Callable[[], float] = time.time,
        request: Callable[..., dict] = _api_request,
        register_secret: Optional[Callable[[str], None]] = None,
    ) -> None:
        if app_id is None or not str(app_id).strip():
            raise ValueError("app_id is required")
        # Parse once at construction: validates the key AND caches (n, d) so every
        # _jwt() signs without re-parsing the PEM (and a bad key fails here, not
        # mid-run).
        self._n, self._d = _parse_rsa_private_key(private_key_pem)
        self._app_id = app_id
        self._api_url = api_url.rstrip("/")
        self._now = now
        self._request = request
        # Optional sink for minted credentials (the App JWT and installation
        # tokens, both bearer creds) so the caller can register them with its
        # redactor — see GitHubAPI.from_app.
        self._register_secret = register_secret
        self._token_cache: dict[int, tuple[str, float]] = {}
        self._install_cache: dict[tuple[str, str], int] = {}

    def _jwt(self) -> str:
        jwt = _sign_jwt(self._app_id, self._n, self._d, now=self._now())
        if self._register_secret is not None:
            self._register_secret(jwt)  # scrub from any later log/comment
        return jwt

    def installation_id_for_repo(self, owner: str, repo: str) -> int:
        """Resolve (and cache) the installation id covering ``owner/repo``."""
        key = (owner, repo)
        cached = self._install_cache.get(key)
        if cached is not None:
            return cached
        data = self._request(
            "GET",
            f"{self._api_url}/repos/{owner}/{repo}/installation",
            jwt=self._jwt(),
        )
        installation_id = int(data["id"])
        self._install_cache[key] = installation_id
        return installation_id

    def installation_token(self, installation_id: int) -> str:
        """Mint (and cache) a scoped access token for one installation."""
        installation_id = int(installation_id)
        cached = self._token_cache.get(installation_id)
        now = self._now()
        if cached is not None and cached[1] > now:
            return cached[0]
        data = self._request(
            "POST",
            f"{self._api_url}/app/installations/{installation_id}/access_tokens",
            jwt=self._jwt(),
        )
        token = data["token"]
        if self._register_secret is not None:
            self._register_secret(token)
        # Cache until ~10 min before the API-reported expiry, capped at 50 min —
        # robust to a shorter-than-usual token lifetime, never serving a stale one.
        ttl = _TOKEN_TTL_SECONDS
        expires = _parse_expires_at(data.get("expires_at"))
        if expires is not None:
            ttl = min(ttl, max(expires - now - _TOKEN_EXPIRY_SKEW, 0.0))
        self._token_cache[installation_id] = (token, now + ttl)
        return token

    def token_for_repo(self, owner: str, repo: str) -> str:
        """Convenience: installation token for the App's install on ``owner/repo``."""
        return self.installation_token(self.installation_id_for_repo(owner, repo))


__all__ = ["GitHubApp", "GitHubAppError", "build_app_jwt", "GITHUB_API"]
