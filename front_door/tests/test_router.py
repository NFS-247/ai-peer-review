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
        return [
            {"number": 114, "title": "Phase 4a", "html_url": "http://x/114", "updated_at": "t"},
            {"number": 200, "title": "WIP refactor", "html_url": "http://x/200", "updated_at": "t"},
        ]

    def list_labels(self, repo, number):
        if number == 114:
            return ["dispatcher:tier-high_stakes", "dispatcher:round-10", "dispatcher:escalated"]
        return ["dispatcher:tier-backend", "dispatcher:round-2"]

    def list_issue_comments(self, repo, number):
        return [_state_comment(5.8)] if number == 114 else []

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


def test_inbox_shows_only_actionable():
    r = route("GET", "/inbox", cookies={}, form={}, deps=_deps(FakeOperator()))
    assert r.status == 200
    assert "#114" in r.body          # escalated -> actionable
    assert "WIP refactor" not in r.body  # reviewing -> not in inbox


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
