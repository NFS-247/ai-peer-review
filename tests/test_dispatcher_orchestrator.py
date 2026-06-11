"""Orchestrator-level tests with a fake GitHub API.

Covers GPT review (3rd round) on PR #74:
- #1 no-secret fail-closed: missing DISPATCHER_VERDICT_SECRET must never mark
  a PR ready, even with approving reviewers and green CI in the same run.
- #2 operator note threaded into the reviewer prompt for INVESTIGATE/DISCUSS.
- #3 daily spend ceiling pauses all in-flight PRs.

We use a fake API that records labels/comments and canned reviewers, so no
network calls happen.
"""

import scripts.dispatcher.main as M
from scripts.dispatcher.ai_client import AIResponse
from scripts.dispatcher.ai_prompt import build_review_prompt
from scripts.dispatcher.config import DispatcherConfig, default_tiers
from scripts.dispatcher.github_api import CheckRun, PRComment


# ---- fakes -----------------------------------------------------------------

class FakeAPI:
    def __init__(self, *, open_prs=None, ledger_total=None):
        self.labels: dict[int, list[str]] = {}
        self.comments: dict[int, list[str]] = {}
        self.reviews: list[tuple] = []
        self._open_prs = open_prs or [101]
        # If ledger_total is set, find_issue_by_marker returns a ledger issue
        # whose events sum to that amount "now", simulating prior spend.
        self._ledger_total = ledger_total
        self._pr = {
            "head": {"sha": "deadbeefcafebabe", "ref": "feature/x"},
            "title": "Test PR",
            "body": "body",
            "html_url": "https://example/pr/101",
            "state": "open",
        }

    # PR data
    def get_pr(self, n): return dict(self._pr)
    def get_pr_diff(self, n): return "diff text"
    def get_pr_files(self, n): return ["README.md"]

    # comments
    def list_pr_comments(self, n):
        out = []
        for i, body in enumerate(self.comments.get(n, [])):
            out.append(PRComment(id=i, body=body, author_login="github-actions[bot]",
                                  author_id=1, created_at=f"2026-01-01T00:00:0{i}Z"))
        return out

    def post_comment(self, n, body):
        self.comments.setdefault(n, []).append(body)
        return {"id": 1}

    # labels
    def list_labels(self, n): return list(self.labels.get(n, []))
    def add_labels(self, n, labels):
        cur = self.labels.setdefault(n, [])
        for l in labels:
            if l not in cur:
                cur.append(l)
        return {}
    def remove_label(self, n, label):
        cur = self.labels.setdefault(n, [])
        if label in cur:
            cur.remove(label)

    # checks
    def list_check_runs(self, sha):
        return [CheckRun(name="pytest", status="completed", conclusion="success")]

    # reviews / lifecycle
    def submit_review(self, n, *, event, body=""):
        self.reviews.append((n, event))
        return {}
    def close_pr(self, n): return {}
    def list_open_pull_numbers(self): return list(self._open_prs)
    def list_open_pulls_with_labels(self):
        return [(n, list(self.labels.get(n, []))) for n in self._open_prs]

    # issues (global spend ledger)
    def find_issue_by_marker(self, marker):
        if self._ledger_total is None:
            return None
        import json
        import time
        events = [{"ts": time.time(), "cost": float(self._ledger_total)}]
        body = (
            f"{marker}\n\n```tradewatcher-global-spend\n"
            f"{json.dumps({'events': events})}\n```\n"
        )
        return {"number": 9999, "body": body}
    def create_issue(self, title, body): return {"number": 9999}
    def update_issue_body(self, num, body): return {}


class FakeClient:
    calls: list = []

    def __init__(self, reviewer): self.reviewer_name = reviewer
    def review(self, prompt):
        FakeClient.calls.append(self.reviewer_name)
        body = '{"verdict": "approve", "reasoning": "ok", "concerns": []}'
        return AIResponse(raw_text=body, model="fake", input_tokens=1,
                          output_tokens=1, cost_usd=0.01)


def _cfg(*, secret, daily_ceiling=20.0):
    return DispatcherConfig(
        project_name="TradeWatcher",
        operator_email="",
        operator_github_login="NERT24",
        repo_owner="NFS-247",
        repo_name="StockTrader",
        anthropic_api_key="a",
        openai_api_key="o",
        gemini_api_key="g",
        resend_api_key=None,
        github_token="t",
        verdict_secret=secret,
        tiers=default_tiers(),
        max_review_rounds=6,
        per_pr_cost_ceiling_usd=5.0,
        daily_cost_ceiling_usd=daily_ceiling,
    )


# ---- #1 no-secret fail-closed ----------------------------------------------

def test_no_secret_never_marks_ready(monkeypatch):
    api = FakeAPI()
    cfg = _cfg(secret=None)
    monkeypatch.setattr(M, "_build_client", lambda r, c: FakeClient(r))

    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    assert M.label_state.LABEL_READY not in api.labels.get(101, [])
    assert "dispatcher:secret-missing" in api.labels.get(101, [])


def test_with_secret_can_mark_ready(monkeypatch):
    api = FakeAPI()
    cfg = _cfg(secret="real-secret")
    monkeypatch.setattr(M, "_build_client", lambda r, c: FakeClient(r))

    # README.md -> routine tier -> reviewers claude+gpt; both approve.
    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    assert M.label_state.LABEL_READY in api.labels.get(101, [])


# ---- #2 operator note threaded into prompt ---------------------------------

def test_operator_note_in_prompt():
    p = build_review_prompt(
        reviewer="claude", pr_number=1, pr_title="t", pr_body="b",
        diff_text="d", tier="routine", round_=2,
        operator_note="focus on the auth change",
    )
    assert "OPERATOR INSTRUCTION FOR THIS ROUND" in p
    assert "focus on the auth change" in p


def test_no_operator_note_no_section():
    p = build_review_prompt(
        reviewer="claude", pr_number=1, pr_title="t", pr_body="b",
        diff_text="d", tier="routine", round_=1,
    )
    assert "OPERATOR INSTRUCTION FOR THIS ROUND" not in p


def test_prompt_injects_project_context():
    # Multi-tenancy: domain framing + bug classes come from config, not a
    # dispatcher hard-code.
    p = build_review_prompt(
        reviewer="gpt", pr_number=2, pr_title="t", pr_body="b", diff_text="d",
        tier="backend", round_=1,
        project_description="a paper-only trading system; it never places broker orders.",
        review_guidance=["Lookahead bias", "Broker order paths"],
    )
    assert "a paper-only trading system" in p
    assert "Specifically for THIS project" in p
    assert "- Lookahead bias" in p
    assert "- Broker order paths" in p


def test_prompt_generic_without_project_context():
    p = build_review_prompt(
        reviewer="gpt", pr_number=2, pr_title="t", pr_body="b", diff_text="d",
        tier="backend", round_=1,
    )
    # No project supplied -> no project-specific section, but still adversarial
    # with the strict JSON schema.
    assert "Specifically for THIS project" not in p
    assert "adversarial review" in p
    assert '"verdict"' in p
    # And no StockTrader leakage in the generic prompt.
    assert "TradeWatcher" not in p
    assert "paper-only" not in p


def test_investigate_threads_note_into_round(monkeypatch):
    api = FakeAPI()
    cfg = _cfg(secret="real-secret")

    captured = {}
    real_prompt = M.build_review_prompt
    def spy_prompt(**kwargs):
        captured["operator_note"] = kwargs.get("operator_note", "")
        return real_prompt(**kwargs)
    monkeypatch.setattr(M, "build_review_prompt", spy_prompt)
    monkeypatch.setattr(M, "_build_client", lambda r, c: FakeClient(r))

    status, note = M._handle_operator_command(
        cfg=cfg, api=api, pr_number=101,
        comment_body="OPERATOR INVESTIGATE check the lookahead window",
        author_user_id=42, operator_user_id=42,
    )
    assert status == M.CMD_RESULT_RUN_ROUND
    assert note == "check the lookahead window"

    M._run_review_round(cfg=cfg, api=api, pr_number=101, operator_note=note)
    assert captured["operator_note"] == "check the lookahead window"


# ---- #3 daily ceiling pauses all in-flight PRs -----------------------------

def test_daily_ceiling_pauses_all_in_flight(monkeypatch):
    # Two open PRs, both with a dispatcher tier label.
    api = FakeAPI(open_prs=[101, 202])
    api.labels[101] = ["dispatcher:tier-routine"]
    api.labels[202] = ["dispatcher:tier-backend"]

    M._pause_all_in_flight(api, reason="test ceiling")

    assert M.label_state.LABEL_PAUSED in api.labels[101]
    assert M.label_state.LABEL_PAUSED in api.labels[202]


def test_pause_all_skips_untracked_prs(monkeypatch):
    api = FakeAPI(open_prs=[101, 303])
    api.labels[101] = ["dispatcher:tier-routine"]
    api.labels[303] = []  # not tracked by the dispatcher

    M._pause_all_in_flight(api, reason="test ceiling")

    assert M.label_state.LABEL_PAUSED in api.labels[101]
    assert M.label_state.LABEL_PAUSED not in api.labels.get(303, [])


# ---- preflight: already over daily budget -> zero reviewer calls -----------

def test_over_budget_preflight_calls_no_reviewers(monkeypatch):
    # The ledger already shows $25 spent in the last 24h; ceiling is $20.
    api = FakeAPI(open_prs=[101, 202], ledger_total=25.0)
    api.labels[202] = ["dispatcher:tier-backend"]
    cfg = _cfg(secret="real-secret", daily_ceiling=20.0)

    FakeClient.calls = []
    monkeypatch.setattr(M, "_build_client", lambda r, c: FakeClient(r))

    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    # ZERO reviewer calls were made despite a fresh event.
    assert FakeClient.calls == []
    # The PR is escalated and all in-flight PRs are paused.
    assert M.label_state.LABEL_ESCALATED in api.labels.get(101, [])
    assert M.label_state.LABEL_PAUSED in api.labels.get(101, [])
    assert M.label_state.LABEL_PAUSED in api.labels.get(202, [])
    # And it never marked ready.
    assert M.label_state.LABEL_READY not in api.labels.get(101, [])


def test_under_budget_preflight_allows_reviewers(monkeypatch):
    # Ledger shows $5 spent; ceiling $20 -> reviewers run normally.
    api = FakeAPI(open_prs=[101], ledger_total=5.0)
    cfg = _cfg(secret="real-secret", daily_ceiling=20.0)

    FakeClient.calls = []
    monkeypatch.setattr(M, "_build_client", lambda r, c: FakeClient(r))

    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    # Reviewers were called (routine tier -> claude + gpt).
    assert set(FakeClient.calls) == {"claude", "gpt"}


# ---- CI status read failure degrades to PENDING (does not crash) -----------

def test_ci_status_read_failure_degrades_to_pending():
    from scripts.dispatcher.converge import CIStatus

    class BoomAPI:
        def list_check_runs(self, sha):
            raise RuntimeError("HTTP 403: Resource not accessible by integration")

    # The live smoke test crashed here with a 403 before checks:read was added.
    # A read failure must now degrade to PENDING, never raise.
    assert M._ci_status_for(BoomAPI(), "abc123") == CIStatus.PENDING


# ---- convergence uses CI status re-fetched AFTER reviews (PR #81 fix) -------

def test_ci_status_fetched_after_reviewer_calls(monkeypatch):
    """CI status is read AFTER the reviewer calls, not before.

    Regression for PR #81: the review round captured CI status once at the
    start (PENDING) and reused it for convergence, so it declined to mark a PR
    ready even though CI had gone green during the (slow) reviewer calls. The
    fix removes the early capture and fetches CI status after the reviewers
    finish. This test records the number of reviewer calls completed at the
    moment list_check_runs is invoked and asserts CI was read only after all
    reviewers had been called.
    """
    from scripts.dispatcher.github_api import CheckRun

    api = FakeAPI()
    cfg = _cfg(secret="real-secret")

    FakeClient.calls = []
    reviewer_count_at_ci_read = []

    def recording_check_runs(sha):
        # Capture how many reviewer calls have happened when CI is read.
        reviewer_count_at_ci_read.append(len(FakeClient.calls))
        return [CheckRun(name="pytest", status="completed", conclusion="success")]

    api.list_check_runs = recording_check_runs  # type: ignore[assignment]
    monkeypatch.setattr(M, "_build_client", lambda r, c: FakeClient(r))

    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    # routine tier -> claude + gpt -> 2 reviewer calls.
    assert FakeClient.calls == ["claude", "gpt"]
    # CI was read at least once, and every read happened AFTER both reviewers.
    assert reviewer_count_at_ci_read, "CI status was never read"
    assert all(n == 2 for n in reviewer_count_at_ci_read)
    # And with CI green + both approving, the PR is marked ready.
    assert M.label_state.LABEL_READY in api.labels.get(101, [])
