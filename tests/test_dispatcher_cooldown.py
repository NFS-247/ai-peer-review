"""Tests for the Cut-1 escalation-cooldown timing fix.

Three layers:
- pure cooldown_elapsed() decision,
- CrossRunState pending-escalation round-trip + back-compat (a state with no
  pending escalation must still verify against pre-Cut-1 signed comments),
- orchestrator behavior: plain reviewer dissent now stays SILENT entirely (the
  dev agent owns iteration); a still-deferring stuck-signal (a non-converged
  head-lock PR) defers, fires once the dev agent goes quiet past the cooldown,
  and is superseded by a new commit; infra/budget escalations ping immediately.
"""

import scripts.dispatcher.main as M
import scripts.dispatcher.post_review as PR
from scripts.dispatcher import state as label_state
from scripts.dispatcher.ai_client import AIResponse
from scripts.dispatcher.config import DispatcherConfig, TierConfig
from scripts.dispatcher.escalation import cooldown_elapsed
from scripts.dispatcher.github_api import CheckRun, PRComment, PRReview
from scripts.dispatcher.repo_config import RepoConfig


def _monotonic_verdict_clock(monkeypatch):
    """Make each posted verdict's emitted_at strictly increasing.

    utc_now_iso() has 1-second resolution; in a test two rounds post within the
    same second, so the convergence tie-break would keep the older (now-stale)
    verdict. In production rounds are minutes apart, so this only fixes the
    test's time compression.
    """
    seq = {"n": 0}

    def _iso():
        seq["n"] += 1
        return f"2026-01-01T00:00:{seq['n']:02d}Z"

    monkeypatch.setattr(PR, "utc_now_iso", _iso)


# ---- pure cooldown_elapsed --------------------------------------------------

def test_cooldown_not_due_before_window():
    assert cooldown_elapsed(
        pending_since=1000.0, pending_head_sha="a", current_head_sha="a",
        now_ts=1000.0 + 5 * 60, cooldown_minutes=10,
    ) is False


def test_cooldown_due_after_window():
    assert cooldown_elapsed(
        pending_since=1000.0, pending_head_sha="a", current_head_sha="a",
        now_ts=1000.0 + 11 * 60, cooldown_minutes=10,
    ) is True


def test_cooldown_superseded_by_new_commit():
    # Head changed since the pending was recorded -> not due (re-armed).
    assert cooldown_elapsed(
        pending_since=1000.0, pending_head_sha="a", current_head_sha="b",
        now_ts=1000.0 + 99 * 60, cooldown_minutes=10,
    ) is False


def test_cooldown_disabled_when_zero():
    assert cooldown_elapsed(
        pending_since=1000.0, pending_head_sha="a", current_head_sha="a",
        now_ts=1000.0 + 99 * 60, cooldown_minutes=0,
    ) is False


def test_cooldown_no_pending():
    assert cooldown_elapsed(
        pending_since=0.0, pending_head_sha="", current_head_sha="a",
        now_ts=9_999_999.0, cooldown_minutes=10,
    ) is False


# ---- CrossRunState round-trip + back-compat --------------------------------

def test_pending_state_roundtrips_and_signs():
    secret = "s3cret"
    s = label_state.CrossRunState(
        cumulative_cost_usd=1.5,
        pending_escalation_since=1234.5,
        pending_escalation_head_sha="abc",
        pending_escalation_trigger="high_stakes_first_dissent",
        pending_escalation_reason_short="dissent",
        pending_escalation_detail="claude requested changes",
        escalated_head_sha="def",
    )
    block = s.to_block(secret)

    class _API:
        def list_pr_comments(self, n):
            return [PRComment(id=1, body=block, author_login="github-actions[bot]",
                              author_id=1, created_at="2026-01-01T00:00:00Z")]

    got = label_state.read_cross_run_state(_API(), 1, "github-actions[bot]", secret)
    assert got.pending_escalation_since == 1234.5
    assert got.pending_escalation_head_sha == "abc"
    assert got.pending_escalation_trigger == "high_stakes_first_dissent"
    assert got.escalated_head_sha == "def"


def test_legacy_state_without_pending_still_verifies():
    # A pre-Cut-1 state comment was signed over only the 3 original fields. The
    # new _payload omits the Cut-1 fields when unset, so the signature still
    # matches and the state is trusted (no reset on the tag bump).
    import hashlib
    import hmac
    import json
    secret = "k"
    legacy_payload = {
        "cumulative_cost_usd": 2.0,
        "ci_fix_attempts": 1,
        "consecutive_api_failures": 0,
    }
    signing = json.dumps(legacy_payload, sort_keys=True, separators=(",", ":"))
    sig = hmac.new(secret.encode(), signing.encode(), hashlib.sha256).hexdigest()
    legacy_payload["signature"] = sig
    body = (
        f"{label_state.STATE_COMMENT_MARKER}\n\n"
        "```tradewatcher-dispatcher-state\n"
        f"{json.dumps(legacy_payload, indent=2)}\n```\n"
    )

    class _API:
        def list_pr_comments(self, n):
            return [PRComment(id=1, body=body, author_login="github-actions[bot]",
                              author_id=1, created_at="2026-01-01T00:00:00Z")]

    got = label_state.read_cross_run_state(_API(), 1, "github-actions[bot]", secret)
    assert got.cumulative_cost_usd == 2.0
    assert got.ci_fix_attempts == 1
    assert got.has_pending_escalation() is False


# ---- orchestrator-level: defer / fire / supersede / immediate ---------------

class FakeAPI:
    def __init__(self, *, files, head="headsha1", ci="success"):
        self.labels: dict[int, list[str]] = {}
        self.comments: dict[int, list[str]] = {}
        self.reviews: list = []
        self.review_records: dict[int, list[PRReview]] = {}
        self._files = files
        self._head = head
        self._ci = ci

    def set_head(self, sha): self._head = sha

    def get_pr(self, n):
        return {"head": {"sha": self._head, "ref": "feature/x"}, "title": "T",
                "body": "b", "html_url": "https://x/pr/101", "state": "open"}

    def get_pr_diff(self, n): return "diff"
    def get_pr_files(self, n): return list(self._files)

    def list_pr_comments(self, n):
        return [PRComment(id=i, body=b, author_login="github-actions[bot]",
                          author_id=1, created_at=f"2026-01-01T00:00:0{i % 10}Z")
                for i, b in enumerate(self.comments.get(n, []))]

    def post_comment(self, n, body):
        self.comments.setdefault(n, []).append(body)
        return {"id": len(self.comments[n])}

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

    def list_check_runs(self, sha):
        return [CheckRun(name="pytest", status="completed", conclusion=self._ci)]

    def submit_review(self, n, *, event, body=""):
        self.reviews.append((n, event))
        state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED"}
        self.review_records.setdefault(n, []).append(
            PRReview(author_login="github-actions[bot]",
                     state=state.get(event, "COMMENTED"), commit_id=self._head))

    def list_pr_reviews(self, n): return list(self.review_records.get(n, []))
    def close_pr(self, n): return {}
    def list_open_pull_numbers(self): return [101]
    def list_open_pulls_with_labels(self): return [(101, list(self.labels.get(101, [])))]
    def find_issue_by_marker(self, marker): return None
    def create_issue(self, t, b): return {"number": 1}
    def update_issue_body(self, num, b): return {}

    # convenience for assertions
    def comment_texts(self, n): return "\n".join(self.comments.get(n, []))


def _cfg(*, cooldown=10, per_pr_ceiling=5.0, reviewers=("claude",), webhook=None,
         head_lock=()):
    tiers = {
        "routine": TierConfig(reviewers=reviewers, round_budget=3),
        "backend": TierConfig(reviewers=reviewers, round_budget=3),
        "high_stakes": TierConfig(reviewers=reviewers, round_budget=2),
    }
    return DispatcherConfig(
        project_name="Canary", operator_email="", operator_github_login="NERT24",
        repo_owner="NFS-247", repo_name="Canary",
        anthropic_api_key="a", openai_api_key="o", gemini_api_key="g",
        resend_api_key=None, github_token="t", verdict_secret="sek",
        google_chat_webhook_url=webhook,
        tiers=tiers, max_review_rounds=6, per_pr_cost_ceiling_usd=per_pr_ceiling,
        daily_cost_ceiling_usd=1000.0, escalation_cooldown_minutes=cooldown,
        repo_config=RepoConfig(head_lock_paths=head_lock),
    )


class DissentClient:
    def __init__(self, reviewer): self.reviewer = reviewer
    def review(self, prompt):
        body = ('{"verdict": "request_changes", "reasoning": "no", '
                '"concerns": [{"file": "x", "line": null, "issue": "bad"}]}')
        return AIResponse(raw_text=body, model="fake", input_tokens=1,
                          output_tokens=1, cost_usd=0.01)


class ApproveClient:
    def __init__(self, reviewer): self.reviewer = reviewer
    def review(self, prompt):
        body = '{"verdict": "approve", "reasoning": "ok", "concerns": []}'
        return AIResponse(raw_text=body, model="fake", input_tokens=1,
                          output_tokens=1, cost_usd=0.01)


def _clock(monkeypatch, holder):
    monkeypatch.setattr(M, "_now_ts", lambda: holder["t"])


def test_plain_dissent_stays_silent(monkeypatch):
    # New model: an unknown path -> high_stakes tier; a single reviewer requests
    # changes on round 1. Pre-Cut-1 this pinged immediately; under Cut-1 it
    # deferred (HIGH_STAKES_FIRST_DISSENT). NOW plain dissent is suppressed
    # entirely — the dev agent owns iteration — so nothing is pinged OR pending.
    api = FakeAPI(files=["weird/unknown.txt"])  # unknown -> high_stakes
    cfg = _cfg(cooldown=10)
    holder = {"t": 1000.0}
    _clock(monkeypatch, holder)
    monkeypatch.setattr(M, "_build_client", lambda r, c: DissentClient(r))

    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    # No escalation label, no operator ping/comment — and crucially nothing
    # queued for a later sweep either.
    assert label_state.LABEL_ESCALATED not in api.labels.get(101, [])
    assert "needs you" not in api.comment_texts(101)
    assert "Escalation" not in api.comment_texts(101)
    cross = label_state.read_cross_run_state(api, 101, "github-actions[bot]", "sek")
    assert cross.has_pending_escalation() is False


# The deferral machinery below is now driven by a still-deferring stuck-signal: a
# NON-converged high-stakes PR touching a head-lock path (HIGH_STAKES_AUTO). It
# needs operator sign-off but the dev agent is still iterating, so it defers until
# the agent goes quiet — exactly the case the cooldown timer exists for.
def test_sweep_does_not_fire_before_cooldown(monkeypatch):
    api = FakeAPI(files=["weird/unknown.txt"])
    cfg = _cfg(cooldown=10, head_lock=("weird/**",))
    holder = {"t": 1000.0}
    _clock(monkeypatch, holder)
    monkeypatch.setattr(M, "_build_client", lambda r, c: DissentClient(r))
    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    holder["t"] = 1000.0 + 5 * 60  # only 5 min quiet
    M._run_cooldown_sweep(cfg=cfg, api=api)
    assert label_state.LABEL_ESCALATED not in api.labels.get(101, [])


def test_sweep_fires_after_cooldown(monkeypatch):
    api = FakeAPI(files=["weird/unknown.txt"])
    cfg = _cfg(cooldown=10, head_lock=("weird/**",))
    holder = {"t": 1000.0}
    _clock(monkeypatch, holder)
    monkeypatch.setattr(M, "_build_client", lambda r, c: DissentClient(r))
    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    holder["t"] = 1000.0 + 11 * 60  # 11 min quiet -> due
    M._run_cooldown_sweep(cfg=cfg, api=api)

    assert label_state.LABEL_ESCALATED in api.labels.get(101, [])
    # No email/webhook -> escalation falls back to a PR comment.
    assert "Escalation" in api.comment_texts(101)
    # Pending cleared after firing.
    cross = label_state.read_cross_run_state(api, 101, "github-actions[bot]", "sek")
    assert cross.has_pending_escalation() is False
    assert cross.escalated_head_sha == "headsha1"


def test_new_commit_resets_timer_no_ping(monkeypatch):
    api = FakeAPI(files=["weird/unknown.txt"])
    cfg = _cfg(cooldown=10, head_lock=("weird/**",))
    holder = {"t": 1000.0}
    _clock(monkeypatch, holder)
    _monotonic_verdict_clock(monkeypatch)
    monkeypatch.setattr(M, "_build_client", lambda r, c: DissentClient(r))
    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    # A new commit lands at t=1300 (within the old window) and re-runs review.
    holder["t"] = 1300.0
    api.set_head("headsha2")
    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    cross = label_state.read_cross_run_state(api, 101, "github-actions[bot]", "sek")
    assert cross.pending_escalation_head_sha == "headsha2"
    assert cross.pending_escalation_since == 1300.0  # timer reset

    # The original window (1000+11min) would be due, but the head moved, so a
    # sweep at 1660 must NOT fire (only ~6 min quiet on head2).
    holder["t"] = 1000.0 + 11 * 60
    M._run_cooldown_sweep(cfg=cfg, api=api)
    assert label_state.LABEL_ESCALATED not in api.labels.get(101, [])


def test_ready_for_merge_pings_chat_all_tiers(monkeypatch):
    # Merge-ready ping fires for a routine/backend PR that converges — not just
    # for high-stakes escalations.
    api = FakeAPI(files=["README.md"])  # routine; single reviewer approves
    cfg = _cfg(cooldown=10, webhook="https://chat.googleapis.com/v1/spaces/x/messages")
    holder = {"t": 1000.0}
    _clock(monkeypatch, holder)
    monkeypatch.setattr(M, "_build_client", lambda r, c: ApproveClient(r))
    sent = []
    monkeypatch.setattr(M, "send_chat_message", lambda url, card, **k: sent.append(card))

    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    assert label_state.LABEL_READY in api.labels.get(101, [])
    assert len(sent) == 1
    assert sent[0]["cardsV2"][0]["card"]["header"]["title"].endswith("ready to merge")


def test_ready_for_merge_does_not_double_ping(monkeypatch):
    api = FakeAPI(files=["README.md"])
    cfg = _cfg(cooldown=10, webhook="https://chat.googleapis.com/v1/spaces/x/messages")
    holder = {"t": 1000.0}
    _clock(monkeypatch, holder)
    monkeypatch.setattr(M, "_build_client", lambda r, c: ApproveClient(r))
    sent = []
    monkeypatch.setattr(M, "send_chat_message", lambda url, card, **k: sent.append(card))

    M._run_review_round(cfg=cfg, api=api, pr_number=101)
    # A later CI event re-evaluates convergence but must NOT ping again.
    M._run_convergence_only(cfg=cfg, api=api, pr_number=101)

    assert len(sent) == 1


def test_immediate_cost_spike_still_pings(monkeypatch):
    # Infra/budget escalations are NOT cooldown-gated: they ping immediately.
    # Use a still-iterating (dissenting) PR so the per-PR cost ceiling actually
    # fires — a CONVERGED PR over budget is intentionally NOT escalated (it is
    # ready for merge; see escalation.test_converged_pr_over_budget_is_ready).
    api = FakeAPI(files=["README.md"])  # routine
    cfg = _cfg(cooldown=10, per_pr_ceiling=0.005)  # one round busts the ceiling
    holder = {"t": 1000.0}
    _clock(monkeypatch, holder)
    monkeypatch.setattr(M, "_build_client", lambda r, c: DissentClient(r))

    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    assert label_state.LABEL_ESCALATED in api.labels.get(101, [])
    assert label_state.LABEL_READY not in api.labels.get(101, [])


def test_converged_unanimous_high_stakes_fires_immediately(monkeypatch):
    # A high-stakes PR that converges unanimously on round 1 triggers
    # SUSPICIOUS_UNANIMOUS — a cooldown-gated trigger. Before the fix it was
    # DEFERRED (queued for a flaky sweep); now, because the PR has CONVERGED,
    # it fires IN THE SAME RUN — no cooldown, no sweep dependency.
    api = FakeAPI(files=["weird/unknown.txt"])  # unknown -> high_stakes
    cfg = _cfg(cooldown=10)
    holder = {"t": 1000.0}
    _clock(monkeypatch, holder)
    monkeypatch.setattr(M, "_build_client", lambda r, c: ApproveClient(r))

    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    # Fired right away — not left pending for a sweep.
    assert label_state.LABEL_ESCALATED in api.labels.get(101, [])
    assert "Escalation" in api.comment_texts(101)
    cross = label_state.read_cross_run_state(api, 101, "github-actions[bot]", "sek")
    assert cross.has_pending_escalation() is False
    assert cross.escalated_head_sha == "headsha1"


def test_converged_head_lock_fires_immediately_and_dedupes(monkeypatch):
    # A converged high-stakes PR touching a head-lock path needs operator sign-off
    # (HIGH_STAKES_AUTO). Before the fix this was deferred to a flaky sweep — the
    # exact path that silently dropped nfs-central #99's ping. Now it fires in the
    # same run, AND a re-delivered CI-complete event must NOT re-ping.
    api = FakeAPI(files=["weird/unknown.txt"])  # high_stakes + matched below
    cfg = _cfg(cooldown=10, head_lock=("weird/**",))
    holder = {"t": 1000.0}
    _clock(monkeypatch, holder)
    monkeypatch.setattr(M, "_build_client", lambda r, c: ApproveClient(r))

    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    # Fired immediately as an ESCALATION (operator sign-off), not marked READY.
    assert label_state.LABEL_ESCALATED in api.labels.get(101, [])
    assert label_state.LABEL_READY not in api.labels.get(101, [])
    fired = api.comment_texts(101).count("Escalation")
    assert fired == 1

    # A re-delivered CI-complete event re-evaluates convergence but must NOT
    # re-ping (escalated_head_sha + escalated-label dedupe).
    M._run_convergence_only(cfg=cfg, api=api, pr_number=101)
    assert api.comment_texts(101).count("Escalation") == 1

    cross = label_state.read_cross_run_state(api, 101, "github-actions[bot]", "sek")
    assert cross.has_pending_escalation() is False
    assert cross.escalated_head_sha == "headsha1"


def test_update_branch_reaffirms_ready_without_rereview(monkeypatch):
    # Branch protection requires "up to date": the operator clicks Update branch ->
    # NEW head, SAME diff. A ready PR must re-affirm (re-submit APPROVE) on the new
    # head WITHOUT a fresh review round (no AI cost), restoring the approval GitHub
    # dismissed so the PR can merge.
    from scripts.dispatcher.verdict import (
        build_verdict, compute_diff_sha256, compute_verdict_signature,
    )
    api = FakeAPI(files=["README.md"])
    cfg = _cfg(cooldown=10)
    diff_sha = compute_diff_sha256(api.get_pr_diff(101))
    # a trusted signed verdict for the CURRENT diff, but an OLD head (pre-update):
    v = build_verdict(reviewer="claude", verdict="approve", pr_number=101,
                      head_sha="oldhead0", diff_sha256=diff_sha, tier="routine", round_=1)
    sig = compute_verdict_signature(v, "sek")
    api.comments[101] = [f"**Review from `claude`**\n\nok\n\n{v.to_yaml_block(signature=sig)}\n"]
    api.add_labels(101, [label_state.LABEL_READY])

    def _boom(r, c):
        raise AssertionError("must not re-review on an update-branch")
    monkeypatch.setattr(M, "_build_client", _boom)

    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    assert (101, "APPROVE") in api.reviews                 # approval re-affirmed
    assert label_state.LABEL_READY in api.labels.get(101, [])
    assert len(api.comments.get(101, [])) == 1             # no new round, no new comments


def test_operator_investigate_bypasses_update_branch_shortcut(monkeypatch):
    # An explicit operator re-review (INVESTIGATE/DISCUSS carries a note) must run a
    # real round even when the PR is ready and the diff is unchanged.
    from scripts.dispatcher.verdict import (
        build_verdict, compute_diff_sha256, compute_verdict_signature,
    )
    api = FakeAPI(files=["README.md"])
    cfg = _cfg(cooldown=10)
    diff_sha = compute_diff_sha256(api.get_pr_diff(101))
    v = build_verdict(reviewer="claude", verdict="approve", pr_number=101,
                      head_sha="oldhead0", diff_sha256=diff_sha, tier="routine", round_=1)
    sig = compute_verdict_signature(v, "sek")
    api.comments[101] = [f"**Review from `claude`**\n\nok\n\n{v.to_yaml_block(signature=sig)}\n"]
    api.add_labels(101, [label_state.LABEL_READY])
    _monotonic_verdict_clock(monkeypatch)
    called = []
    monkeypatch.setattr(M, "_build_client", lambda r, c: called.append(r) or ApproveClient(r))

    M._run_review_round(cfg=cfg, api=api, pr_number=101, operator_note="re-check the importer")

    assert called == ["claude"]   # reviewers WERE invoked despite ready + same diff


def test_update_branch_shortcut_requires_approve_verdict(monkeypatch):
    # Round-1 panel concern (all three reviewers): a trusted verdict on the same
    # diff proves the diff was REVIEWED, not APPROVED. If the only verdict for
    # the current diff_sha256 is request_changes, the shortcut must NOT fire —
    # even with LABEL_READY present (stale/operator-applied) — and a normal
    # round runs instead.
    from scripts.dispatcher.verdict import (
        build_verdict, compute_diff_sha256, compute_verdict_signature,
    )
    api = FakeAPI(files=["README.md"])
    cfg = _cfg(cooldown=10)
    diff_sha = compute_diff_sha256(api.get_pr_diff(101))
    v = build_verdict(reviewer="claude", verdict="request_changes", pr_number=101,
                      head_sha="oldhead0", diff_sha256=diff_sha, tier="routine", round_=1)
    sig = compute_verdict_signature(v, "sek")
    api.comments[101] = [f"**Review from `claude`**\n\nno\n\n{v.to_yaml_block(signature=sig)}\n"]
    api.add_labels(101, [label_state.LABEL_READY])
    _monotonic_verdict_clock(monkeypatch)
    called = []
    monkeypatch.setattr(M, "_build_client", lambda r, c: called.append(r) or DissentClient(r))

    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    assert called == ["claude"]                       # real round, not the shortcut
    assert (101, "APPROVE") not in api.reviews        # and no bot APPROVE appeared


def test_update_branch_reaffirm_survives_override_dissent(monkeypatch):
    # The PR's whole point (nfs-central #107): gpt dissented, the operator
    # overrode to ready, then Update branch dismissed the approval. An approve
    # verdict on the same diff exists (claude's), so the override survives —
    # re-affirm fires, with no fresh round.
    from scripts.dispatcher.verdict import (
        build_verdict, compute_diff_sha256, compute_verdict_signature,
    )
    api = FakeAPI(files=["README.md"])
    cfg = _cfg(cooldown=10)
    diff_sha = compute_diff_sha256(api.get_pr_diff(101))
    comments = []
    for reviewer, verdict in (("claude", "approve"), ("gpt", "request_changes")):
        v = build_verdict(reviewer=reviewer, verdict=verdict, pr_number=101,
                          head_sha="oldhead0", diff_sha256=diff_sha, tier="routine", round_=1)
        sig = compute_verdict_signature(v, "sek")
        comments.append(f"**Review from `{reviewer}`**\n\n.\n\n{v.to_yaml_block(signature=sig)}\n")
    api.comments[101] = comments
    api.add_labels(101, [label_state.LABEL_READY])

    def _boom(r, c):
        raise AssertionError("must not re-review on an update-branch")
    monkeypatch.setattr(M, "_build_client", _boom)

    M._run_review_round(cfg=cfg, api=api, pr_number=101)

    assert (101, "APPROVE") in api.reviews


def test_update_branch_shortcut_idempotent_across_reruns(monkeypatch):
    # Round-1 panel concern (claude): a workflow re-run on a head that was
    # already re-affirmed must not double-post APPROVE. The dismissed approval
    # keeps the OLD head's commit_id, so "APPROVED by us on the CURRENT head"
    # is the restore-needed signal — present means do nothing.
    from scripts.dispatcher.verdict import (
        build_verdict, compute_diff_sha256, compute_verdict_signature,
    )
    api = FakeAPI(files=["README.md"])
    cfg = _cfg(cooldown=10)
    diff_sha = compute_diff_sha256(api.get_pr_diff(101))
    v = build_verdict(reviewer="claude", verdict="approve", pr_number=101,
                      head_sha="oldhead0", diff_sha256=diff_sha, tier="routine", round_=1)
    sig = compute_verdict_signature(v, "sek")
    api.comments[101] = [f"**Review from `claude`**\n\nok\n\n{v.to_yaml_block(signature=sig)}\n"]
    api.add_labels(101, [label_state.LABEL_READY])

    def _boom(r, c):
        raise AssertionError("must not re-review on an update-branch")
    monkeypatch.setattr(M, "_build_client", _boom)

    M._run_review_round(cfg=cfg, api=api, pr_number=101)
    M._run_review_round(cfg=cfg, api=api, pr_number=101)   # re-run, same head

    assert api.reviews.count((101, "APPROVE")) == 1        # exactly once
    assert len(api.comments.get(101, [])) == 1             # and still no new comments
