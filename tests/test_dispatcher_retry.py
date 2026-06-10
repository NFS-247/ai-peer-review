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
