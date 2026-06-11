"""Tests for github_api efficiency + resilience (rate-limit prevention).

Covers the three changes that keep the dispatcher under GitHub's per-repo
request budget: labels come back with the PR list (no per-PR label call), the
ledger lookup is memoized (no repeated full-issue scans), and _request retries
transient rate limits instead of failing the round.
"""

import email.message
import urllib.error

from scripts.dispatcher import github_api as gh
from scripts.dispatcher.github_api import GitHubAPI


class _Resp:
    def __init__(self, data: bytes):
        self._d = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._d


# ---- (A) labels come from the PR list -------------------------------------
def test_list_open_pulls_with_labels_parses_inline_labels(monkeypatch):
    api = GitHubAPI("t", "o", "r")
    paths = []

    def fake_request(method, path, **kw):
        paths.append(path)
        if "/pulls?state=open" in path:
            return [
                {"number": 1, "labels": [{"name": "dispatcher:tier-routine"}, {"name": "x"}]},
                {"number": 2, "labels": []},
            ]
        return []

    monkeypatch.setattr(api, "_request", fake_request)
    result = api.list_open_pulls_with_labels()
    assert result == [(1, ["dispatcher:tier-routine", "x"]), (2, [])]
    # No per-PR label round-trip — that's the whole point.
    assert all("/labels" not in p for p in paths)


# ---- (B) ledger lookup is memoized ----------------------------------------
def test_find_issue_by_marker_memoizes_number(monkeypatch):
    api = GitHubAPI("t", "o", "r")
    paths = []

    def fake_request(method, path, **kw):
        paths.append(path)
        if "/issues?state=open" in path:
            return [{"number": 9999, "body": "MARK here"}]
        if path.endswith("/issues/9999"):
            return {"number": 9999, "body": "MARK here (fresh)"}
        return None

    monkeypatch.setattr(api, "_request", fake_request)
    first = api.find_issue_by_marker("MARK")
    second = api.find_issue_by_marker("MARK")

    assert first["number"] == 9999 and second["number"] == 9999
    # Listed all issues exactly once; the second call fetched by number (fresh).
    assert sum(1 for p in paths if "issues?state=open" in p) == 1
    assert any(p.endswith("/issues/9999") for p in paths)
    assert "fresh" in second["body"]  # by-number GET returns the live body


def test_find_issue_by_marker_rescans_when_cache_stale(monkeypatch):
    api = GitHubAPI("t", "o", "r")
    api._marker_issue_cache["MARK"] = 5  # pretend we cached a now-wrong number

    def fake_request(method, path, **kw):
        if path.endswith("/issues/5"):
            return {"number": 5, "body": "no marker here anymore"}
        if "/issues?state=open" in path:
            return [{"number": 9999, "body": "MARK here"}]
        return None

    monkeypatch.setattr(api, "_request", fake_request)
    found = api.find_issue_by_marker("MARK")
    assert found["number"] == 9999  # fell back to a scan and relearned


# ---- (C) _request retries transient rate limits ---------------------------
def _patch_sleep(monkeypatch):
    monkeypatch.setattr(gh, "_sleep", lambda s: None)


def test_request_retries_on_429(monkeypatch):
    _patch_sleep(monkeypatch)
    api = GitHubAPI("t", "o", "r")
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", email.message.Message(), None)
        return _Resp(b'{"ok": true}')

    monkeypatch.setattr(gh.urllib.request, "urlopen", fake_urlopen)
    assert api._request("GET", "/x") == {"ok": True}
    assert calls["n"] == 2  # retried once, then succeeded


def test_request_retries_on_403_rate_limit(monkeypatch):
    _patch_sleep(monkeypatch)
    api = GitHubAPI("t", "o", "r")
    calls = {"n": 0}
    hdrs = email.message.Message()
    hdrs["X-RateLimit-Remaining"] = "0"

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 403, "rate limited", hdrs, None)
        return _Resp(b'{"ok": 1}')

    monkeypatch.setattr(gh.urllib.request, "urlopen", fake_urlopen)
    assert api._request("GET", "/x") == {"ok": 1}
    assert calls["n"] == 2


def test_request_does_not_retry_plain_403(monkeypatch):
    _patch_sleep(monkeypatch)
    api = GitHubAPI("t", "o", "r")
    calls = {"n": 0}

    def fake_urlopen(req, timeout=0):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", email.message.Message(), None)

    monkeypatch.setattr(gh.urllib.request, "urlopen", fake_urlopen)
    try:
        api._request("GET", "/x")
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "403" in str(exc)
    assert calls["n"] == 1  # a genuine permission 403 is not retried
