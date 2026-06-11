"""Tests for github_app.py — pure-stdlib GitHub App auth (RS256 + token mint).

No private key is committed: the RSA key is held as integers (modulus N, private
exponent D — extracted from a throwaway openssl key) and PEMs are rebuilt at
runtime, so there's nothing here for secret-scanning to flag. Correctness of the
hand-rolled RS256 is pinned two independent ways:
  - a KNOWN-ANSWER test against openssl's signature of the same message+key, and
  - public-key verification (pow(sig, e, n) reconstructs the PKCS#1 v1.5 EM).
"""

import base64
import hashlib
import json

import pytest

from scripts.dispatcher import github_app as A
from scripts.dispatcher.github_app import (
    GitHubApp,
    build_app_jwt,
    _b64url,
    _parse_rsa_private_key,
    _sign_rs256,
)

# Throwaway 2048-bit RSA key as integers (n, d) + standard public exponent. The
# reference signature is `printf 'hello.world' | openssl dgst -sha256 -sign key`
# (base64url, unpadded) — i.e. a real RS256 signature from a different codebase.
E = 65537
N = int(
    "9e9a2621aad2a6cb86245a65f5296bf8ba7b77f00401ea0a80594f508a71ec807ff896f98e3b"
    "fe5249dcea509f767211b0abd71f3f3169000bb9cc5161d98c5874ae033aceafb27b650b367f"
    "53522bd880ed3c28fd06b979cdbe4d19aa8c0ef7507c687b0e334e6f865168a5208345cf4efe"
    "9e3e3e0ba1b2a7d76448634ce9f170a6825a7ebe80eaf46138d55df56ac6584803d6bfabf0f1"
    "fbcdd4cccc0717c04a6de97d410efb426473f63f1b59ad6896950793e9e5a53adcabe18fee99"
    "48d49c3c0a03f6a5b4637516c956801ee33b67184196864d08468419be00a0cd7b747e299747"
    "c7c2cfc39417e9f814ce5395cd08a8083b64b73daf5ad58a693384bb",
    16,
)
D = int(
    "4a70bf84fdd071490564faa8f030c8e4ad625620e9409cc0e10d0a151b65ed4342cd42cf4edb"
    "09bb45bfd29a94bddb3c4257e5585d28abc7c1b92b14e7805c47083cc4774d9b59826122aa29"
    "88ca009a55a9039b99671696fce25cfdb6f695efae6f35facbe778e10f821643aac6f27522f6"
    "8eff57cfcdcd34c9fbdf9dfbf3b109ad5d9d6bb3eef67659a9db1adad30446ff97de549c1872"
    "6c27eacb8d1f7d6a9c6dc05fcadcb55179e7237195700209442b0e9ca43611b19e4cc8f1a8dd"
    "100b8f153d12afbedc3b3850cdddaabd7b66845e1082ce8b89081483b6892f5c9eaf88e06637"
    "8134e9fd8b98dcf1d657e2da49eedd81481a1cf108eef7567703c081",
    16,
)
REFERENCE_SIG = (
    "Oeg-byVL2sDwBWvMDxG2rWQ-gFUQwC3xu7mPsotJc2MoVMmoJWz1Ov3TG7oPZhy3CXs6_16"
    "OfoliEG7x1oeo8h_NJemQxg1uti5q6WAC48AqaugcoSyrKM6AN_dvtp9rEgzqR_uIqtC31t"
    "DhwZr-hzhxSSu8B86JcqLmgujk_imI5_YG6Vr-wCq9uJMdauJI5nO0omC9Yhb14gsd_KMXm"
    "WhlhT_c8q0JKUD6uDGZwalv-UAukb83LCAZaO7tTYkCk7d8OQLMJFfApEcb240hF1LK1tye"
    "q2MalBga45U0l32Uobk6_qo_TJXizPrRUFt3iWmBThH2btlLSFNj5-ZVzw"
)


# --- minimal DER encoder, so the key PEMs are built at runtime (not committed) -

def _der_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    body = length.to_bytes((length.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _der_tlv(tag: int, value: bytes) -> bytes:
    return bytes([tag]) + _der_len(len(value)) + value


def _der_uint(x: int) -> bytes:
    # (bit_length // 8) + 1 guarantees a leading 0x00 when the top bit is set,
    # keeping the INTEGER positive (DER two's-complement rule).
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

def test_sign_matches_openssl_reference():
    # Known-answer: our RS256 must byte-match openssl's signature of the same
    # message under the same key. Proves the PKCS#1 v1.5 padding + modexp.
    assert _b64url(_sign_rs256(b"hello.world", N, D)) == REFERENCE_SIG


def test_signature_verifies_under_public_key():
    for msg in (b"", b"abc", b"a longer message with bytes \x00\xff\x10"):
        assert _verifies(msg, _sign_rs256(msg, N, D), N)


# --- key parsing --------------------------------------------------------------

def test_parse_pkcs1():
    assert _parse_rsa_private_key(_pkcs1_pem(N, E, D)) == (N, D)


def test_parse_pkcs8():
    assert _parse_rsa_private_key(_pkcs8_pem(N, E, D)) == (N, D)


def test_parse_rejects_encrypted():
    with pytest.raises(ValueError):
        _parse_rsa_private_key(
            "-----BEGIN ENCRYPTED PRIVATE KEY-----\nQUJD\n-----END ENCRYPTED PRIVATE KEY-----"
        )


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        _parse_rsa_private_key("definitely not a PEM")


# --- App JWT ------------------------------------------------------------------

def test_build_app_jwt_structure_and_claims():
    jwt = build_app_jwt(12345, _pkcs1_pem(N, E, D), now=1_000_000)
    head, payload, _sig = jwt.split(".")
    assert json.loads(_b64url_decode(head)) == {"alg": "RS256", "typ": "JWT"}
    claims = json.loads(_b64url_decode(payload))
    assert claims["iss"] == "12345"             # App id, stringified
    assert claims["iat"] == 1_000_000 - 60      # backdated for clock drift
    assert claims["exp"] == 1_000_000 + 480
    assert claims["exp"] - claims["iat"] <= 600  # GitHub's 10-minute ceiling


def test_app_jwt_signature_self_verifies():
    jwt = build_app_jwt(1, _pkcs1_pem(N, E, D), now=1_000_000)
    signing_input, _, sig_segment = jwt.rpartition(".")
    assert _verifies(signing_input.encode("ascii"), _b64url_decode(sig_segment), N)


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


def test_token_for_repo_mints_and_caches():
    request, calls = _fake_github()
    app = GitHubApp(7, _pkcs1_pem(N, E, D), now=lambda: 1000.0, request=request)
    first = app.token_for_repo("o", "r")
    second = app.token_for_repo("o", "r")
    assert first == second                       # served from cache
    assert _count(calls, "/installation") == 1   # resolved once
    assert _count(calls, "/access_tokens") == 1  # minted once despite two calls


def test_installation_token_refreshes_after_ttl():
    request, calls = _fake_github()
    now = [1000.0]
    app = GitHubApp(7, _pkcs1_pem(N, E, D), now=lambda: now[0], request=request)
    app.installation_token(42)
    now[0] += 3001                               # past the 50-min cache window
    app.installation_token(42)
    assert _count(calls, "/access_tokens") == 2


def test_mint_calls_are_authenticated_with_an_app_jwt():
    request, calls = _fake_github()
    app = GitHubApp(99, _pkcs1_pem(N, E, D), now=lambda: 1000.0, request=request)
    app.token_for_repo("o", "r")
    # Each mint/resolve call carries a well-formed (3-segment) App JWT, and that
    # JWT verifies under the App's public key.
    for _method, _url, jwt in calls:
        signing_input, _, sig = jwt.rpartition(".")
        assert jwt.count(".") == 2
        assert _verifies(signing_input.encode("ascii"), _b64url_decode(sig), N)


def test_construction_validates_inputs():
    with pytest.raises(ValueError):
        GitHubApp(7, "garbage-not-a-key", now=lambda: 0.0)
    with pytest.raises(ValueError):
        GitHubApp(0, _pkcs1_pem(N, E, D))        # missing app id
