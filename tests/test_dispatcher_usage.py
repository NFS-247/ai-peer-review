"""Tests for per-tenant usage metering + billing (usage.py / usage_ledger.py)."""

from scripts.dispatcher import usage
from scripts.dispatcher import usage_ledger as UL
from scripts.dispatcher.config import load_from_env


# ---- pure billing math ------------------------------------------------------

def test_normalize_billing_mode():
    assert usage.normalize_billing_mode("Platform") == "platform"
    assert usage.normalize_billing_mode("  BYOK ") == "byok"
    assert usage.normalize_billing_mode("nonsense") == "byok"  # safe default
    assert usage.normalize_billing_mode("") == "byok"


def test_usage_charge_platform_applies_markup():
    assert usage.compute_usage_charge(0.10, billing_mode="platform", markup_multiplier=1.3) == 0.13


def test_usage_charge_byok_is_zero():
    # Tenant paid the provider directly; platform charges nothing for usage.
    assert usage.compute_usage_charge(0.10, billing_mode="byok", markup_multiplier=1.3) == 0.0


def test_usage_charge_never_negative():
    assert usage.compute_usage_charge(-5, billing_mode="platform", markup_multiplier=1.3) == 0.0


def test_billable_amount_platform_is_usage_plus_devfee():
    # 0.10 * 1.3 = 0.13, + 5.00 flat fee
    assert usage.billable_amount(
        0.10, billing_mode="platform", markup_multiplier=1.3, dev_fee_usd=5.0
    ) == 5.13


def test_billable_amount_byok_is_devfee_only():
    # AI cost is the tenant's own; platform takes only the flat service fee.
    assert usage.billable_amount(
        0.10, billing_mode="byok", markup_multiplier=1.3, dev_fee_usd=5.0
    ) == 5.0


def test_usage_event_roundtrip():
    ev = usage.UsageEvent("claude", "claude-x", 100, 50, 0.012)
    assert usage.UsageEvent.from_dict(ev.to_dict()) == ev


def test_summary_aggregates_by_provider():
    s = usage.summarize([
        usage.UsageEvent("claude", "m", 10, 5, 0.01),
        usage.UsageEvent("gpt", "m", 20, 7, 0.02),
        usage.UsageEvent("claude", "m", 1, 1, 0.03),
    ])
    assert s.calls == 3
    assert s.total_cost_usd == 0.06
    assert s.by_provider["claude"]["calls"] == 2
    assert s.by_provider["claude"]["cost_usd"] == 0.04
    assert s.by_provider["gpt"]["cost_usd"] == 0.02


# ---- ledger pure apply ------------------------------------------------------

def _ev(reviewer="claude", cost=0.01):
    return usage.UsageEvent(reviewer, "m", 1, 1, cost)


def test_apply_events_accumulates_and_charges_platform():
    led = UL.apply_events(
        UL._empty_ledger(), [_ev(cost=0.10), _ev("gpt", 0.10)],
        billing_mode="platform", markup_multiplier=2.0, dev_fee_usd=1.0, now_ts=1000.0,
    )
    assert led["summary"]["total_cost_usd"] == 0.20
    assert led["usage_charge_usd"] == 0.40           # 0.20 * 2.0
    assert led["billable_usd"] == 1.40               # + 1.0 dev fee
    assert led["billing"]["mode"] == "platform"
    assert len(led["recent_events"]) == 2


def test_apply_events_byok_charges_zero_usage():
    led = UL.apply_events(
        UL._empty_ledger(), [_ev(cost=0.50)],
        billing_mode="byok", markup_multiplier=2.0, dev_fee_usd=3.0, now_ts=1.0,
    )
    assert led["summary"]["total_cost_usd"] == 0.50
    assert led["usage_charge_usd"] == 0.0
    assert led["billable_usd"] == 3.0                # dev fee only


def test_recent_events_are_capped_but_totals_authoritative():
    led = UL._empty_ledger()
    events = [_ev(cost=0.001) for _ in range(UL.MAX_RECENT_EVENTS + 50)]
    led = UL.apply_events(
        led, events, billing_mode="platform", markup_multiplier=1.0,
        dev_fee_usd=0.0, now_ts=1.0,
    )
    assert len(led["recent_events"]) == UL.MAX_RECENT_EVENTS  # capped
    assert led["summary"]["calls"] == UL.MAX_RECENT_EVENTS + 50  # total survives cap


# ---- ledger persistence + signing ------------------------------------------

class FakeAPI:
    def __init__(self):
        self.issue = None

    def find_issue_by_marker(self, marker):
        if self.issue and marker in self.issue["body"]:
            return self.issue
        return None

    def create_issue(self, title, body):
        self.issue = {"number": 1, "title": title, "body": body}
        return self.issue

    def update_issue_body(self, num, body):
        self.issue["body"] = body
        return self.issue


def test_record_creates_then_accumulates_across_calls():
    api = FakeAPI()
    secret = "sek"
    UL.record(api, [_ev(cost=0.10)], billing_mode="platform",
              markup_multiplier=1.5, dev_fee_usd=0.0, secret=secret, now_ts=1.0)
    UL.record(api, [_ev("gpt", 0.10)], billing_mode="platform",
              markup_multiplier=1.5, dev_fee_usd=0.0, secret=secret, now_ts=2.0)
    led = UL.read(api, secret)
    assert led["summary"]["total_cost_usd"] == 0.20
    assert led["usage_charge_usd"] == 0.30           # 0.20 * 1.5, running
    assert led["summary"]["calls"] == 2


def test_tampered_ledger_is_rejected():
    api = FakeAPI()
    secret = "sek"
    UL.record(api, [_ev(cost=0.10)], billing_mode="platform",
              markup_multiplier=1.5, dev_fee_usd=0.0, secret=secret, now_ts=1.0)
    # A tenant edits the stored cost down to understate usage.
    api.issue["body"] = api.issue["body"].replace("0.1", "0.0")
    led = UL.read(api, secret)
    # Signature no longer matches -> rejected, reads as empty (not trusted).
    assert led["summary"]["total_cost_usd"] == 0.0


# ---- config wiring ----------------------------------------------------------

def test_billing_config_defaults_are_noop():
    cfg = load_from_env({"GITHUB_TOKEN": "t"})
    assert cfg.billing_mode == "byok"
    assert cfg.usage_markup_multiplier == 1.0
    assert cfg.dev_fee_usd == 0.0


def test_billing_config_env_override():
    cfg = load_from_env({
        "GITHUB_TOKEN": "t",
        "BILLING_MODE": "Platform",
        "USAGE_MARKUP_MULTIPLIER": "1.3",
        "DEV_FEE_USD": "5",
    })
    assert cfg.billing_mode == "platform"
    assert cfg.usage_markup_multiplier == 1.3
    assert cfg.dev_fee_usd == 5.0


def test_billing_config_from_repo_config_file(tmp_path):
    import json
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "billing_mode": "platform",
        "usage_markup_multiplier": 1.25,
        "dev_fee_usd": 2.5,
    }), encoding="utf-8")
    from scripts.dispatcher.config import REPO_CONFIG_PATH_ENV
    cfg = load_from_env({"GITHUB_TOKEN": "t", REPO_CONFIG_PATH_ENV: str(p)})
    assert cfg.billing_mode == "platform"
    assert cfg.usage_markup_multiplier == 1.25
    assert cfg.dev_fee_usd == 2.5
