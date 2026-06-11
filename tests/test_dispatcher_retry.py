"""Tests for request_json_with_retry (transient-failure resilience)."""

import urllib.error
from unittest import mock

import pytest

from scripts.dispatcher import ai_client as A


class _Resp:
    def __init__(self, body=b'{"ok": true}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _http_error(code, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError(
        url="http://x", code=code, msg="err", hdrs=headers, fp=None
    )


def test_succeeds_first_try():
    slept = []
    with mock.patch.object(A.urllib.request, "urlopen", return_value=_Resp()):
        out = A.request_json_with_retry(
            object(), provider="X", sleep=slept.append
        )
    assert out == {"ok": True}
    assert slept == []  # no backoff needed


def test_default_read_timeout_is_generous_and_passed_through():
    # claude + gemini call request_json_with_retry WITHOUT an explicit timeout, so
    # the shared default IS their read window. It must be the generous 300s (the
    # GPT client's default), not the old 120s that reported a slow-but-healthy
    # reviewer on a large diff as "unavailable" and escalated a false alarm.
    assert A.DEFAULT_READ_TIMEOUT == 300
    seen = {}

    def fake_urlopen(req, timeout=0):
        seen["timeout"] = timeout
        return _Resp()

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        A.request_json_with_retry(object(), provider="gemini")
    assert seen["timeout"] == 300


def test_retries_429_then_succeeds():
    calls = []
    slept = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        if len(calls) < 3:
            raise _http_error(429)
        return _Resp()

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        out = A.request_json_with_retry(
            object(), provider="X", base_delay=1.0, sleep=slept.append
        )
    assert out == {"ok": True}
    assert len(calls) == 3          # failed twice, succeeded on the third
    assert slept == [1.0, 2.0]      # exponential backoff between attempts


def test_honors_retry_after_header():
    calls = []
    slept = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, retry_after=7)
        return _Resp()

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        A.request_json_with_retry(object(), provider="X", sleep=slept.append)
    assert slept == [7.0]  # used Retry-After, not the default backoff


def test_gives_up_after_max_attempts_and_raises():
    slept = []

    def fake_urlopen(req, timeout=0):
        raise _http_error(429)

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        with pytest.raises(RuntimeError) as ei:
            A.request_json_with_retry(
                object(), provider="Anthropic", max_attempts=4, sleep=slept.append
            )
    assert "Anthropic API HTTP 429" in str(ei.value)
    assert len(slept) == 3  # slept between the 4 attempts


def test_non_retryable_status_raises_immediately():
    slept = []

    def fake_urlopen(req, timeout=0):
        raise _http_error(400)

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        with pytest.raises(RuntimeError) as ei:
            A.request_json_with_retry(object(), provider="X", sleep=slept.append)
    assert "HTTP 400" in str(ei.value)
    assert slept == []  # 4xx (non-429) is not retried


def test_5xx_not_retried_to_avoid_double_billing():
    # 500/502/504 may have been processed (and billed) server-side; retrying
    # without an idempotency key could double-charge, so they raise at once.
    slept = []

    def fake_urlopen(req, timeout=0):
        raise _http_error(500)

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        with pytest.raises(RuntimeError):
            A.request_json_with_retry(object(), provider="X", sleep=slept.append)
    assert slept == []


def test_retry_after_is_capped_at_max_delay():
    # A hostile/buggy upstream sending a huge Retry-After must not stall the job.
    calls = []
    slept = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise _http_error(429, retry_after=86400)  # 24h
        return _Resp()

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        A.request_json_with_retry(
            object(), provider="X", max_delay=60.0, sleep=slept.append
        )
    assert slept == [60.0]  # capped, not 86400


def test_retries_once_on_read_timeout_then_succeeds():
    # A read timeout surfaces as socket.timeout / TimeoutError, which is NOT a
    # URLError — it would escape the loop and fail the round. One bounded retry
    # recovers a slow-but-healthy generation (the gpt long-review case).
    calls = []
    slept = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("The read operation timed out")
        return _Resp()

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        out = A.request_json_with_retry(
            object(), provider="OpenAI", timeout_retries=1, timeout_backoff=3.0,
            sleep=slept.append,
        )
    assert out == {"ok": True}
    assert len(calls) == 2     # original + one retry
    assert slept == [3.0]      # one short backoff, not the exponential 429 schedule


def test_socket_timeout_is_caught_like_timeout_error():
    # socket.timeout is an alias of TimeoutError on 3.10+, but pin the contract
    # so a future split (or an older runtime) still retries the read timeout.
    calls = []
    slept = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise A.socket.timeout("timed out")
        return _Resp()

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        out = A.request_json_with_retry(
            object(), provider="OpenAI", timeout_retries=1, timeout_backoff=1.0,
            sleep=slept.append,
        )
    assert out == {"ok": True}
    assert len(calls) == 2


def test_gives_up_after_timeout_retries_and_raises():
    # Exhausting the timeout budget declares the reviewer unavailable with a
    # message that names the timeout (so the escalation says why), not a bare
    # opaque error.
    calls = []
    slept = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        raise TimeoutError("timed out")

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        with pytest.raises(RuntimeError) as ei:
            A.request_json_with_retry(
                object(), provider="OpenAI", timeout=300,
                timeout_retries=1, timeout_backoff=3.0, sleep=slept.append,
            )
    assert "read timed out" in str(ei.value)
    assert "OpenAI" in str(ei.value)
    assert "300s" in str(ei.value)
    assert len(calls) == 2     # original + one retry, then give up
    assert slept == [3.0]


def test_timeout_retries_zero_fails_immediately():
    # timeout_retries=0 means no extra attempt: the first read timeout raises.
    calls = []
    slept = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        raise TimeoutError("timed out")

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        with pytest.raises(RuntimeError):
            A.request_json_with_retry(
                object(), provider="X", timeout_retries=0, sleep=slept.append
            )
    assert len(calls) == 1
    assert slept == []


def test_timeout_backoff_capped_at_max_delay():
    # The short timeout backoff is still capped, consistent with the 429 path,
    # so a misconfigured backoff can't stall the job.
    calls = []
    slept = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("timed out")
        return _Resp()

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        A.request_json_with_retry(
            object(), provider="X", timeout_retries=1, timeout_backoff=999.0,
            max_delay=60.0, sleep=slept.append,
        )
    assert slept == [60.0]


def test_timeout_does_not_consume_urlerror_budget():
    # Budget separation: a read timeout must not reduce the 429/URLError attempt
    # budget or shift its exponential-backoff index. One timeout followed by
    # URLErrors must still get the FULL max_attempts budget with an unshifted
    # backoff schedule (exponent starting at 0, not bumped by the timeout).
    calls = []
    slept = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("read timed out")        # one timeout first
        raise urllib.error.URLError("connection refused")  # then URLErrors

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        with pytest.raises(RuntimeError) as ei:
            A.request_json_with_retry(
                object(), provider="X", max_attempts=4, base_delay=1.0,
                timeout_retries=1, timeout_backoff=0.5, sleep=slept.append,
            )
    assert "request failed" in str(ei.value)        # exhausted the URLError budget
    # 1 timeout + a full 4-attempt URLError budget = 5 urlopen calls.
    assert len(calls) == 5
    # Backoff: the timeout's own short backoff, then URLError 1x/2x/4x — the
    # URLError exponent is NOT shifted by the preceding timeout.
    assert slept == [0.5, 1.0, 2.0, 4.0]


def test_urlerror_wrapped_timeout_uses_timeout_budget():
    # urllib can wrap a read/connect timeout as URLError(reason=timeout). It must
    # route to the SEPARATE timeout budget (one retry here), not consume the
    # 4-attempt URLError budget.
    calls = []
    slept = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        raise urllib.error.URLError(TimeoutError("timed out"))

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        with pytest.raises(RuntimeError) as ei:
            A.request_json_with_retry(
                object(), provider="X", timeout_retries=1, timeout_backoff=0.5,
                sleep=slept.append,
            )
    assert "read timed out" in str(ei.value)
    assert len(calls) == 2      # original + ONE timeout retry, not max_attempts
    assert slept == [0.5]


def test_string_reason_timeout_is_classified_as_timeout():
    # Some stacks raise URLError with a plain string reason "timed out".
    calls = []
    slept = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        raise urllib.error.URLError("timed out")

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        with pytest.raises(RuntimeError) as ei:
            A.request_json_with_retry(
                object(), provider="X", timeout_retries=1, timeout_backoff=0.5,
                sleep=slept.append,
            )
    assert "read timed out" in str(ei.value)
    assert len(calls) == 2
    assert slept == [0.5]


def test_timeout_message_omits_duration_when_timeout_none():
    # Non-GPT callers may leave timeout at urllib's default (None); the failure
    # message must not render "after Nones".
    slept = []

    def fake_urlopen(req, timeout=0):
        raise TimeoutError("timed out")

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        with pytest.raises(RuntimeError) as ei:
            A.request_json_with_retry(
                object(), provider="X", timeout=None, timeout_retries=0,
                sleep=slept.append,
            )
    msg = str(ei.value)
    assert "read timed out" in msg
    assert "None" not in msg
    assert slept == []          # timeout_retries=0 → no retry, fails cleanly


def test_non_retryable_http_raises_on_first_call():
    # A non-retryable HTTP code (400) raises on the first urlopen, consuming no
    # retry budget — so a future refactor can't silently start retrying it.
    calls = []
    slept = []

    def fake_urlopen(req, timeout=0):
        calls.append(1)
        raise _http_error(400)

    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        with pytest.raises(RuntimeError) as ei:
            A.request_json_with_retry(object(), provider="X", sleep=slept.append)
    assert "HTTP 400" in str(ei.value)
    assert len(calls) == 1
    assert slept == []


def test_same_request_reused_across_timeout_retry():
    # The retry must reuse the SAME Request object across attempts, so a header
    # the caller set once (e.g. OpenAI's Idempotency-Key) is identical on every
    # attempt and the provider can dedupe a retried-but-processed request.
    req = A.urllib.request.Request(
        "http://x", headers={"Idempotency-Key": "air-xyz"}
    )
    seen = []
    calls = []

    def fake_urlopen(r, timeout=0):
        calls.append(1)
        seen.append(r.get_header("Idempotency-key"))
        if len(calls) == 1:
            raise TimeoutError("read timed out")
        return _Resp()

    slept = []
    with mock.patch.object(A.urllib.request, "urlopen", fake_urlopen):
        out = A.request_json_with_retry(
            req, provider="X", timeout_retries=1, timeout_backoff=0.1,
            sleep=slept.append,
        )
    assert out == {"ok": True}
    assert len(calls) == 2
    assert seen == ["air-xyz", "air-xyz"]  # same Request → same key each attempt
