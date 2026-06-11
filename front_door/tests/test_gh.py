"""GitHub client tests — urlopen mocked, no network."""

import io
import json
from unittest import mock
from urllib.parse import parse_qs, urlparse

from front_door.app import gh


class _Resp:
    def __init__(self, payload):
        self._data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._data


def test_list_labels_parses_names():
    payload = [{"name": "dispatcher:tier-routine"}, {"name": "dispatcher:ready-for-merge"}]
    with mock.patch.object(gh.urllib.request, "urlopen", lambda req, timeout=0: _Resp(payload)):
        labels = gh.GitHub("tok").list_labels("o/r", 5)
    assert labels == ["dispatcher:tier-routine", "dispatcher:ready-for-merge"]


def test_post_issue_comment_sends_auth_and_body():
    captured = {}

    def fake_urlopen(req, timeout=0):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["auth"] = req.get_header("Authorization")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"id": 1})

    with mock.patch.object(gh.urllib.request, "urlopen", fake_urlopen):
        gh.GitHub("optoken").post_issue_comment("o/r", 114, "OPERATOR APPROVE")

    assert captured["method"] == "POST"
    assert captured["url"].endswith("/repos/o/r/issues/114/comments")
    assert captured["auth"] == "Bearer optoken"        # operator identity
    assert captured["body"] == {"body": "OPERATOR APPROVE"}


def test_list_open_pulls_paginates_until_short_page():
    # Page 1 is full (100) so the client must fetch page 2; page 2 is short so it
    # stops. A naive per_page=100-only read would have missed the page-2 PRs.
    pages = {1: [{"number": i} for i in range(100)],
             2: [{"number": 100}, {"number": 101}]}
    seen = []

    def fake_urlopen(req, timeout=0):
        q = parse_qs(urlparse(req.full_url).query)
        page = int(q.get("page", ["1"])[0])
        seen.append(page)
        return _Resp(pages.get(page, []))

    with mock.patch.object(gh.urllib.request, "urlopen", fake_urlopen):
        pulls = gh.GitHub("t").list_open_pulls("o/r")
    assert [p["number"] for p in pulls] == list(range(102))
    assert seen == [1, 2]  # stopped after the short page, didn't loop forever


def test_find_issue_body_by_marker_skips_prs():
    issues = [
        {"body": "a PR", "pull_request": {"url": "x"}},   # must be skipped
        {"body": "ledger <!-- tradewatcher-dispatcher-global-spend --> here"},
    ]
    with mock.patch.object(gh.urllib.request, "urlopen", lambda req, timeout=0: _Resp(issues)):
        body = gh.GitHub("t").find_issue_body_by_marker("o/r", "<!-- tradewatcher-dispatcher-global-spend -->")
    assert "ledger" in body
