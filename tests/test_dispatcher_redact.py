"""Tests for scripts.dispatcher.redact and the GitHub output choke points.

Triggered by the live smoke test (PR #77/#79), where API keys leaked into PR
comments inside exception strings. Redaction must scrub them, and it must do
so at the actual public-output choke points, not only in the pure helper.

All credential-looking values in this file are SYNTHETIC — assembled from
fragments at runtime (e.g. "sk-ant-" + "A" * 32) so the test file never
contains incident-looking or scanner-tripping credential material.
"""

import pytest

from scripts.dispatcher import redact as R
from scripts.dispatcher.github_api import GitHubAPI


# Synthetic credential fixtures. None of these are real keys; each is a prefix
# plus a repeated filler character long enough to trigger the pattern (20+).
SYN_ANTHROPIC = "sk-ant-" + "A" * 40
SYN_OPENAI_PROJ = "sk-proj-" + "B" * 40
SYN_OPENAI_BEARER = "Bearer " + "sk-proj-" + "C" * 40
SYN_GOOGLE = "AIza" + "D" * 35
SYN_RESEND = "re_" + "E" * 30
SYN_GHP = "ghp_" + "F" * 36


@pytest.fixture(autouse=True)
def _clear():
    R.clear_registered()
    yield
    R.clear_registered()


# ---- pure helper: pattern-based --------------------------------------------

def test_redacts_anthropic_pattern():
    out = R.redact(f"Invalid header value b'{SYN_ANTHROPIC}'")
    assert "sk-ant-" + "A" * 40 not in out
    assert R.REDACTED in out


def test_redacts_openai_bearer_keeps_word_bearer():
    out = R.redact(SYN_OPENAI_BEARER)
    assert "C" * 40 not in out
    assert "Bearer" in out  # keep the word, drop the token
    assert R.REDACTED in out


def test_redacts_google_pattern():
    out = R.redact(f"key={SYN_GOOGLE}")
    assert "AIza" + "D" * 35 not in out


def test_redacts_resend_pattern():
    out = R.redact(f"{SYN_RESEND} failed")
    assert "E" * 30 not in out


def test_redacts_github_pat_pattern():
    out = R.redact(f"token {SYN_GHP} used")
    assert "F" * 36 not in out


# ---- pure helper: registered exact values ----------------------------------

def test_registered_exact_value_redacted_even_if_unusual_format():
    secret = "custom-" + "z" * 30
    R.register_secret(secret)
    out = R.redact(f"the secret was {secret} oops")
    assert secret not in out
    assert R.REDACTED in out


def test_registered_handles_trailing_newline_form():
    secret = "mykey-" + "q" * 20
    R.register_secret(secret + "\n")
    out = R.redact(f"here is {secret} in text")
    assert secret not in out


def test_short_value_not_registered():
    R.register_secret("abc")  # too short to register
    assert R.redact("abc is fine and common") == "abc is fine and common"


def test_ordinary_text_untouched():
    text = "This is a normal review comment with no secrets."
    assert R.redact(text) == text


def test_empty_text():
    assert R.redact("") == ""


# ---- choke point: GitHubAPI.post_comment / submit_review -------------------

class _CapturingAPI(GitHubAPI):
    """GitHubAPI subclass that captures the request body instead of sending."""

    def __init__(self):
        super().__init__(token="t", owner="o", repo="r")
        self.captured = []

    def _request(self, method, path, body=None, accept=None, raw=False):  # type: ignore[override]
        self.captured.append({"method": method, "path": path, "body": body})
        return {}


def test_post_comment_redacts_at_choke_point():
    secret = "sk-ant-" + "G" * 40
    R.register_secret(secret)
    api = _CapturingAPI()
    api.post_comment(1, f"reviewer failed: Invalid header value '{secret}'")
    sent = api.captured[-1]["body"]["body"]
    assert secret not in sent
    assert R.REDACTED in sent


def test_post_comment_redacts_unregistered_key_shape():
    # Even without registering, a key-shaped token is scrubbed by pattern.
    api = _CapturingAPI()
    token = "sk-proj-" + "H" * 40
    api.post_comment(2, f"oops {token}")
    sent = api.captured[-1]["body"]["body"]
    assert token not in sent


def test_submit_review_redacts_at_choke_point():
    secret = "re_" + "J" * 30
    R.register_secret(secret)
    api = _CapturingAPI()
    api.submit_review(3, event="COMMENT", body=f"context leaked {secret}")
    sent = api.captured[-1]["body"]["body"]
    assert secret not in sent
    assert R.REDACTED in sent
