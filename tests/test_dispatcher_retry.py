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
            object(), provider="OpenAI", timeout_backoff=3.0, sleep=slept.append
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
            object(), provider="OpenAI", timeout_backoff=1.0, sleep=slept.append
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
            object(), provider="X", timeout_backoff=999.0, max_delay=60.0,
            sleep=slept.append,
        )
    assert slept == [60.0]
