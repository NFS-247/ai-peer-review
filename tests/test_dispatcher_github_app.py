"""Tests for github_app.py — pure-stdlib GitHub App auth (RS256 + token mint).

No private key material is committed. The RS256 path is pinned two ways:
  - the EM/padding is cross-checked against OpenSSL using PUBLIC values only —
    verifying OpenSSL's reference signature under the public modulus reconstructs
    our PKCS#1 v1.5 EM (no private exponent needed); and
  - the full sign→verify round-trip uses a throwaway RSA key generated *at runtime*
    (stdlib Miller-Rabin), so nothing private is ever stored in the tree.
"""

import base64
import hashlib
import io
import json
import secrets

import pytest

from scripts.dispatcher import github_app as A
from scripts.dispatcher.github_app import (
    GitHubApp,
    build_app_jwt,
    _b64url,
    _parse_rsa_private_key,
    _sign_rs256,
)

E = 65537

# PUBLIC modulus of a throwaway OpenSSL key + OpenSSL's RS256 signature of
# b"hello.world" (base64url, unpadded). Both are public values — there is no
# private exponent here — used only to pin our EM construction to OpenSSL.
OPENSSL_N = int(
    "9e9a2621aad2a6cb86245a65f5296bf8ba7b77f00401ea0a80594f508a71ec807ff896f98e3b"
    "fe5249dcea509f767211b0abd71f3f3169000bb9cc5161d98c5874ae033aceafb27b650b367f"
    "53522bd880ed3c28fd06b979cdbe4d19aa8c0ef7507c687b0e334e6f865168a5208345cf4efe"
    "9e3e3e0ba1b2a7d76448634ce9f170a6825a7ebe80eaf46138d55df56ac6584803d6bfabf0f1"
    "fbcdd4cccc0717c04a6de97d410efb426473f63f1b59ad6896950793e9e5a53adcabe18fee99"
    "48d49c3c0a03f6a5b4637516c956801ee33b67184196864d08468419be00a0cd7b747e299747"
    "c7c2cfc39417e9f814ce5395cd08a8083b64b73daf5ad58a693384bb",
    16,
)
REFERENCE_SIG = (
    "Oeg-byVL2sDwBWvMDxG2rWQ-gFUQwC3xu7mPsotJc2MoVMmoJWz1Ov3TG7oPZhy3CXs6_16"
    "OfoliEG7x1oeo8h_NJemQxg1uti5q6WAC48AqaugcoSyrKM6AN_dvtp9rEgzqR_uIqtC31t"
    "DhwZr-hzhxSSu8B86JcqLmgujk_imI5_YG6Vr-wCq9uJMdauJI5nO0omC9Yhb14gsd_KMXm"
    "WhlhT_c8q0JKUD6uDGZwalv-UAukb83LCAZaO7tTYkCk7d8OQLMJFfApEcb240hF1LK1tye"
    "q2MalBga45U0l32Uobk6_qo_TJXizPrRUFt3iWmBThH2btlLSFNj5-ZVzw"
)


# --- ephemeral RSA key, generated at runtime (nothing committed) --------------

def _is_probable_prime(n: int, rounds: int = 40) -> bool:
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits: int) -> int:
    while True:
        cand = secrets.randbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(cand):
            return cand


def _gen_rsa_key(bits: int = 768) -> tuple[int, int, int]:
    """Throwaway RSA key (n, e, d) minted in-process — 768-bit is ample for RS256."""
    e = 65537
    while True:
        p, q = _gen_prime(bits // 2), _gen_prime(bits // 2)
        if p == q:
            continue
        phi = (p - 1) * (q - 1)
        try:
            return p * q, e, pow(e, -1, phi)
        except ValueError:  # e not invertible mod phi; vanishingly rare, retry
            continue


KN, KE, KD = _gen_rsa_key()


# --- minimal DER encoder, so the key PEMs are built at runtime (not committed) -

def _der_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _der_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(value)) + value


def _der_uint(x: int) -> bytes:
    return _der_tlv(0x02, x.to_bytes((x.bit_length() // 8) + 1, "big"))


def _der_seq(*elements: bytes) -> bytes:
    return _der_tlv(0x30, b"".join(elements))


def _pem(label: str, der: bytes) -> str:
    body = base64.encodebytes(der).decode("ascii").strip()
    return f"-----BEGIN {label}-----\n{body}\n-----END {label}-----\n"


def _pkcs1_pem(n: int, e: int, d: int) -> str:
    der = _der_seq(_der_uint(0), _der_uint(n), _der_uint(e), _der_uint(d))
    return _pem("RSA PRIVATE KEY", der)


def _pkcs8_pem(n: int, e: int, d: int) -> str:
    inner = _der_seq(_der_uint(0), _der_uint(n), _der_uint(e), _der_uint(d))
    algorithm = _der_seq(bytes.fromhex("06092a864886f70d010101"), bytes.fromhex("0500"))
    info = _der_seq(_der_uint(0), algorithm, _der_tlv(0x04, inner))  # OCTET STRING
    return _pem("PRIVATE KEY", info)


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _expected_em_int(message: bytes, n: int) -> int:
    digest_info = A._SHA256_DIGESTINFO_PREFIX + hashlib.sha256(message).digest()
    k = (n.bit_length() + 7) // 8
    em = b"\x00\x01" + b"\xff" * (k - len(digest_info) - 3) + b"\x00" + digest_info
    return int.from_bytes(em, "big")


def _verifies(message: bytes, signature: bytes, n: int, e: int = E) -> bool:
    """RSA public-key verify: sig^e mod n must reconstruct the PKCS#1 v1.5 EM."""
    return pow(int.from_bytes(signature, "big"), e, n) == _expected_em_int(message, n)


# --- signing correctness ------------------------------------------------------

def test_em_construction_matches_openssl():
    # Verify OpenSSL's reference signature under the PUBLIC modulus: sig^e mod n
    # must equal our PKCS#1 v1.5 EM for SHA-256("hello.world"). Pins our padding /
    # DigestInfo to OpenSSL using public values only.
    sig_int = int.from_bytes(_b64url_decode(REFERENCE_SIG), "big")
    assert pow(sig_int, E, OPENSSL_N) == _expected_em_int(b"hello.world", OPENSSL_N)


def test_sign_then_verify_roundtrip():
    for msg in (b"", b"abc", b"a longer message with bytes \x00\xff\x10"):
        assert _verifies(msg, _sign_rs256(msg, KN, KD), KN)


# --- key parsing --------------------------------------------------------------

def test_parse_pkcs1():
    assert _parse_rsa_private_key(_pkcs1_pem(KN, KE, KD)) == (KN, KD)


def test_parse_pkcs8():
    assert _parse_rsa_private_key(_pkcs8_pem(KN, KE, KD)) == (KN, KD)


def test_parse_rejects_encrypted():
    with pytest.raises(ValueError):
        _parse_rsa_private_key(
            "-----BEGIN ENCRYPTED PRIVATE KEY-----\nQUJD\n-----END ENCRYPTED PRIVATE KEY-----"
        )


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        _parse_rsa_private_key("definitely not a PEM")


def test_parse_rejects_truncated_der():
    # SEQUENCE header claims 256 bytes but only 2 follow — must surface a clear
    # ValueError, not an IndexError leaking out of the parser.
    truncated = _pem("RSA PRIVATE KEY", b"\x30\x82\x01\x00\x02\x01")
    with pytest.raises(ValueError):
        _parse_rsa_private_key(truncated)


def test_der_rejects_indefinite_length():
    with pytest.raises(ValueError):
        A._der_read_tlv(bytes([0x30, 0x80, 0x00, 0x00]), 0)


# --- App JWT ------------------------------------------------------------------

def test_build_app_jwt_structure_and_claims():
    jwt = build_app_jwt(12345, _pkcs1_pem(KN, KE, KD), now=1_000_000)
    head, payload, _sig = jwt.split(".")
    assert json.loads(_b64url_decode(head)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_b64url_decode(payload))
    assert claims["iss"] == "12345"             # App id, stringified
    assert claims["iat"] == 1_000_000 - 60      # backdated for clock drift
    assert claims["exp"] == 1_000_000 + 480
    assert claims["exp"] - claims["iat"] <= 600  # GitHub's 10-minute ceiling


def test_app_jwt_signature_self_verifies():
    jwt = build_app_jwt(1, _pkcs1_pem(KN, KE, KD), now=1_000_000)
    signing_input, _, sig_segment = jwt.rpartition(".")
    assert _verifies(signing_input.encode("ascii"), _b64url_decode(sig_segment), KN)


# --- installation token minting ----------------------------------------------

def _fake_github(installation_id: int = 42):
    calls = []

    def request(method, url, *, jwt, body=None):
        calls.append((method, url, jwt))
        if url.endswith("/installation"):
            return {"id": installation_id}
        if url.endswith("/access_tokens"):
            minted = sum(1 for c in calls if c[1].endswith("/access_tokens"))
            return {"token": f"ghs_inst_{minted}"}
        raise AssertionError(f"unexpected request: {url}")

    return request, calls


def _count(calls, suffix):
    return sum(1 for c in calls if c[1].endswith(suffix))


def _app(**kw):
    return GitHubApp(7, _pkcs1_pem(KN, KE, KD), **kw)


def test_token_for_repo_mints_and_caches():
    request, calls = _fake_github()
    app = _app(now=lambda: 1000.0, request=request)
    first = app.token_for_repo("o", "r")
    second = app.token_for_repo("o", "r")
    assert first == second                       # served from cache
    assert _count(calls, "/installation") == 1   # resolved once
    assert _count(calls, "/access_tokens") == 1  # minted once despite two calls


def test_installation_token_refreshes_after_ttl():
    request, calls = _fake_github()
    now = [1000.0]
    app = _app(now=lambda: now[0], request=request)
    app.installation_token(42)
    now[0] += 3001                               # past the 50-min cap
    app.installation_token(42)
    assert _count(calls, "/access_tokens") == 2


def test_installation_token_caps_ttl_at_expires_at():
    from datetime import datetime, timezone

    now = [1000.0]
    mints = []

    def request(method, url, *, jwt, body=None):
        if not url.endswith("/access_tokens"):
            raise AssertionError(url)
        mints.append(1)
        # Reported expiry is only 8 min out — inside the 10-min skew, so the
        # token must be treated as due and re-minted on the next call.
        exp = datetime.fromtimestamp(now[0] + 480, tz=timezone.utc).isoformat()
        return {"token": "ghs_x", "expires_at": exp}

    app = _app(now=lambda: now[0], request=request)
    app.installation_token(42)
    app.installation_token(42)
    assert len(mints) == 2


def test_mint_calls_are_authenticated_with_an_app_jwt():
    request, calls = _fake_github()
    app = _app(now=lambda: 1000.0, request=request)
    app.token_for_repo("o", "r")
    for _method, _url, jwt in calls:
        signing_input, _, sig = jwt.rpartition(".")
        assert jwt.count(".") == 2
        assert _verifies(signing_input.encode("ascii"), _b64url_decode(sig), KN)


def test_jwt_and_token_registered_for_redaction():
    request, _calls = _fake_github()
    registered = []
    app = _app(now=lambda: 1000.0, request=request, register_secret=registered.append)
    token = app.token_for_repo("o", "r")
    assert token in registered                       # the installation token
    assert any(s.count(".") == 2 for s in registered)  # the App JWT(s)


def test_api_request_wraps_http_error_without_leaking_jwt(monkeypatch):
    def boom(req, timeout=0):
        raise A.urllib.error.HTTPError(
            req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"message":"no install"}')
        )

    monkeypatch.setattr(A.urllib.request, "urlopen", boom)
    with pytest.raises(A.GitHubAppError) as ei:
        A._api_request(
            "GET", "https://api.github.com/repos/o/r/installation", jwt="header.payload.sig"
        )
    assert ei.value.status == 404                     # 4xx classified as misconfig
    message = str(ei.value)
    assert "404" in message
    assert "header.payload.sig" not in message       # the App JWT must not leak


def test_api_request_wraps_network_error_with_no_status(monkeypatch):
    def boom(req, timeout=0):
        raise A.urllib.error.URLError("connection refused")

    monkeypatch.setattr(A.urllib.request, "urlopen", boom)
    with pytest.raises(A.GitHubAppError) as ei:
        A._api_request("GET", "https://api.github.com/x", jwt="h.p.s")
    assert ei.value.status is None                    # transport error -> no status


def test_construction_validates_inputs():
    with pytest.raises(ValueError):
        GitHubApp(7, "garbage-not-a-key", now=lambda: 0.0)   # unparseable key
    for bad_app_id in ("", "   ", None):
        with pytest.raises(ValueError):
            GitHubApp(bad_app_id, _pkcs1_pem(KN, KE, KD))     # empty / missing id


def test_pkcs8_rejects_non_rsa_oid():
    # An EC/Ed25519 PKCS#8 key would otherwise parse as RSA and yield nonsense;
    # the OID check turns it into a clear misconfig error.
    inner = _der_seq(_der_uint(0), _der_uint(KN), _der_uint(KE), _der_uint(KD))
    ec_alg = _der_seq(bytes.fromhex("06072a8648ce3d0201"))  # id-ecPublicKey OID
    info = _der_seq(_der_uint(0), ec_alg, _der_tlv(0x04, inner))
    with pytest.raises(ValueError):
        _parse_rsa_private_key(_pem("PRIVATE KEY", info))


def test_parse_expires_at_handles_z_suffix():
    # GitHub returns RFC3339 with a trailing 'Z'; fromisoformat rejects it before
    # Python 3.11, so it is normalized to +00:00.
    z = A._parse_expires_at("2026-06-11T12:00:00Z")
    offset = A._parse_expires_at("2026-06-11T12:00:00+00:00")
    assert z is not None and z == offset


def test_jwt_is_cached_within_validity():
    registered = []
    now = [1000.0]
    request, _calls = _fake_github()
    app = _app(now=lambda: now[0], request=request, register_secret=registered.append)
    first = app._jwt()
    app._jwt()                                       # within window -> cached, no re-sign
    now[0] += 421                                    # past the ~7-min cache window
    third = app._jwt()
    assert third != first
    assert registered == [first, third]              # signed/registered once per JWT


def test_api_request_malformed_json_is_transient(monkeypatch):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"<html>not json</html>"

    monkeypatch.setattr(A.urllib.request, "urlopen", lambda req, timeout=0: _Resp())
    with pytest.raises(A.GitHubAppError) as ei:
        A._api_request("POST", "https://api.github.com/x", jwt="h.p.s")
    assert ei.value.status is None                   # malformed response -> transient
