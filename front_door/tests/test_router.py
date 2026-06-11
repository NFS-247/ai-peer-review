"""Router tests with injected fakes — no network, no server."""

import json

from front_door.app import config as config_mod
from front_door.app.router import Deps, route


REPO = "NFS-247/StockTrader"
NOW = 1_000_000.0


def _state_comment(cost):
    return {"body": "<!-- tradewatcher-dispatcher-state -->\n```x\n"
            f'{{"cumulative_cost_usd": {cost}}}\n```\n'}


def _ledger(events):
    return ("<!-- tradewatcher-dispatcher-global-spend -->\n```g\n"
            + json.dumps({"events": events}) + "\n```\n")


class FakeRead:
    """A board: PR #114 escalated, PR #200 still reviewing."""

    def list_open_pulls(self, repo):
        # Labels ride on the pulls payload (as the real GitHub API returns them);
        # _gather reads them from here, not via a separate list_labels call.
        return [
            {"number": 114, "title": "Phase 4a", "html_url": "http://x/114", "updated_at": "t",
             "labels": [{"name": "dispatcher:tier-high_stakes"},
                        {"name": "dispatcher:round-10"}, {"name": "dispatcher:escalated"}]},
            {"number": 200, "title": "WIP refactor", "html_url": "http://x/200", "updated_at": "t",
             "labels": [{"name": "dispatcher:tier-backend"}, {"name": "dispatcher:round-2"}]},
        ]

    def list_issue_comments_for_repo(self, repo):
        # One repo-wide sweep; each comment carries the issue_url _gather groups by.
        c = dict(_state_comment(5.8))
        c["issue_url"] = f"https://api.github.com/repos/{repo}/issues/114"
        return [c]

    def find_issue_body_by_marker(self, repo, marker):
        return _ledger([{"ts": NOW - 50, "cost": 5.8, "by": {"claude": 5.0, "gpt": 0.8}}])


class FakeOperator:
    def __init__(self):
        self.posted = []

    def post_issue_comment(self, repo, number, body):
        self.posted.append((repo, number, body))
        return {"id": 1}


def _cfg():
    return config_mod.Config(read_token="t", repos=(REPO,))


def _deps(operator):
    return Deps(read=FakeRead(), operator_client=lambda cookies: operator, cfg=_cfg(), now_ts=NOW)


def test_board_renders_projects_and_spend():
    r = route("GET", "/", cookies={}, form={}, deps=_deps(FakeOperator()))
    assert r.status == 200
    assert REPO in r.body
    assert "Phase 4a" in r.body and "WIP refactor" in r.body
    assert "$5.80" in r.body  # 24h spend / cost
    assert "not cryptographically verified" in r.body  # trust caveat on displayed data


def test_inbox_shows_only_actionable():
    r = route("GET", "/inbox", cookies={}, form={}, deps=_deps(FakeOperator()))
    assert r.status == 200
    assert "#114" in r.body          # escalated -> actionable
    assert "WIP refactor" not in r.body  # reviewing -> not in inbox
    assert "not cryptographically verified" in r.body  # trust caveat at the decision point


def test_action_posts_operator_command_as_operator():
    op = FakeOperator()
    r = route("POST", "/action", cookies={},
              form={"repo": REPO, "number": "114", "action": "approve"}, deps=_deps(op))
    assert r.status == 303 and r.headers["Location"] == "/inbox"
    assert op.posted == [(REPO, 114, "OPERATOR APPROVE")]


def test_action_block_carries_reason():
    op = FakeOperator()
    route("POST", "/action", cookies={},
          form={"repo": REPO, "number": "114", "action": "block", "text": "unsafe broker path"},
          deps=_deps(op))
    assert op.posted == [(REPO, 114, "OPERATOR BLOCK unsafe broker path")]


def test_action_rejects_unknown_repo():
    op = FakeOperator()
    r = route("POST", "/action", cookies={},
              form={"repo": "evil/repo", "number": "1", "action": "approve"}, deps=_deps(op))
    assert r.status == 400
    assert op.posted == []  # never posted to a non-configured repo


def test_action_requires_operator_identity():
    deps = Deps(read=FakeRead(), operator_client=lambda cookies: None, cfg=_cfg(), now_ts=NOW)
    r = route("POST", "/action", cookies={},
              form={"repo": REPO, "number": "114", "action": "approve"}, deps=deps)
    assert r.status == 401


def test_action_block_without_text_is_rejected():
    op = FakeOperator()
    r = route("POST", "/action", cookies={},
              form={"repo": REPO, "number": "114", "action": "block", "text": ""}, deps=_deps(op))
    assert r.status == 400
    assert op.posted == []


def test_unknown_path_404():
    assert route("GET", "/nope", cookies={}, form={}, deps=_deps(FakeOperator())).status == 404


class _BoomRead(FakeRead):
    """One repo reads fine; another raises (bad token / unreachable)."""
    def list_open_pulls(self, repo):
        if repo == "NFS-247/Boom":
            raise RuntimeError("HTTP 404")
        return super().list_open_pulls(repo)


def test_board_isolates_a_failing_repo():
    cfg = config_mod.Config(read_token="t", repos=(REPO, "NFS-247/Boom"))
    deps = Deps(read=_BoomRead(), operator_client=lambda c: FakeOperator(), cfg=cfg, now_ts=NOW)
    r = route("GET", "/", cookies={}, form={}, deps=deps)
    assert r.status == 200                      # the page still renders
    assert "Phase 4a" in r.body                 # the healthy repo's PRs show
    assert "Couldn't read this repo" in r.body  # the bad repo shows an error, not a 500
    assert "HTTP 404" not in r.body             # but the raw detail is NOT leaked to the page


def test_board_fetches_comments_once_per_repo_not_per_pr():
    """gemini round-2 concern: no per-PR comment fan-out. A repo's comments are
    read in a single sweep regardless of how many PRs it has."""
    calls = {"sweep": 0, "per_pr": 0}

    class CountingRead(FakeRead):
        def list_issue_comments_for_repo(self, repo):
            calls["sweep"] += 1
            return super().list_issue_comments_for_repo(repo)

        def list_issue_comments(self, repo, number):
            calls["per_pr"] += 1
            return []

    deps = Deps(read=CountingRead(), operator_client=lambda c: FakeOperator(),
                cfg=_cfg(), now_ts=NOW)
    r = route("GET", "/", cookies={}, form={}, deps=deps)
    assert r.status == 200 and "$5.80" in r.body   # cost still resolved from the sweep
    assert calls["sweep"] == 1                       # one sweep for the repo
    assert calls["per_pr"] == 0                      # never a per-PR comment call (2 PRs)


def test_comment_grouping_by_issue_url():
    from front_door.app.router import _group_comments_by_pr, _issue_number
    assert _issue_number({"issue_url": "https://api.github.com/repos/o/r/issues/114"}) == 114
    assert _issue_number({"issue_url": ".../issues/114/"}) == 114
    assert _issue_number({}) == 0
    g = _group_comments_by_pr([
        {"issue_url": ".../issues/1", "body": "a"},
        {"issue_url": ".../issues/1", "body": "b"},
        {"issue_url": ".../issues/2", "body": "c"},
        {"body": "no url -> dropped"},
    ])
    assert [x["body"] for x in g[1]] == ["a", "b"] and g[2][0]["body"] == "c"
    assert 0 not in g
