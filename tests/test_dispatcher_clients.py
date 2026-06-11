"""Client cost-accounting tests: prompt-cache discounts are applied.

The review prompt (mostly the diff) is re-sent every round, so providers serve
the repeated prefix from cache and bill it cheaply. These tests pin that the
clients read each provider's cache fields and discount accordingly, so the spend
ledger reflects the real (lower) bill rather than full-rating every token.
"""

from scripts.dispatcher import call_claude, call_gemini, call_gpt


def test_gpt_discounts_cached_tokens(monkeypatch):
    payload = {
        "choices": [{"message": {"content": "{}"}}],
        "usage": {
            "prompt_tokens": 1_000_000,
            "completion_tokens": 0,
            "prompt_tokens_details": {"cached_tokens": 800_000},
        },
    }
    monkeypatch.setattr(
        call_gpt, "request_json_with_retry", lambda req, provider, timeout=None: payload
    )
    resp = call_gpt.GPTClient(api_key="k", model="gpt-5").review("p")
    # 200k fresh + 800k cached(0.5x) at $1.25/M input.
    expected = round((200_000 * 1.25 + 800_000 * 1.25 * 0.5) / 1_000_000, 6)
    assert resp.cost_usd == expected
    assert resp.input_tokens == 1_000_000  # total still recorded for the ledger
    # Sanity: cheaper than if all input were full-rated.
    assert resp.cost_usd < round(1_000_000 * 1.25 / 1_000_000, 6)


def test_gemini_discounts_cached_content_tokens(monkeypatch):
    payload = {
        "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
        "usageMetadata": {
            "promptTokenCount": 1_000_000,
            "candidatesTokenCount": 0,
            "cachedContentTokenCount": 600_000,
        },
    }
    monkeypatch.setattr(call_gemini, "request_json_with_retry", lambda req, provider: payload)
    resp = call_gemini.GeminiClient(api_key="k", model="gemini-2.5-pro").review("p")
    # gemini-2.5-pro input $1.25/M, cache 0.25x. 400k fresh + 600k cached.
    expected = round((400_000 * 1.25 + 600_000 * 1.25 * 0.25) / 1_000_000, 6)
    assert resp.cost_usd == expected


def test_claude_accounts_cache_read_and_write(monkeypatch):
    payload = {
        "content": [{"type": "text", "text": "{}"}],
        "usage": {
            "input_tokens": 100_000,
            "output_tokens": 0,
            "cache_read_input_tokens": 500_000,
            "cache_creation_input_tokens": 200_000,
        },
    }
    monkeypatch.setattr(call_claude, "request_json_with_retry", lambda req, provider: payload)
    resp = call_claude.ClaudeClient(api_key="k", model="claude-opus-4-7").review("p")
    # opus input $15/M; fresh 100k + read 500k(0.1x) + write 200k(1.25x).
    expected = round(
        (100_000 * 15 + 500_000 * 15 * 0.1 + 200_000 * 15 * 1.25) / 1_000_000, 6
    )
    assert resp.cost_usd == expected


def test_no_cache_fields_is_plain_cost(monkeypatch):
    # A response with no cache details bills exactly tokens × rate (unchanged).
    payload = {
        "choices": [{"message": {"content": "{}"}}],
        "usage": {"prompt_tokens": 40_000, "completion_tokens": 1_000},
    }
    monkeypatch.setattr(
        call_gpt, "request_json_with_retry", lambda req, provider, timeout=None: payload
    )
    resp = call_gpt.GPTClient(api_key="k", model="gpt-5").review("p")
    assert resp.cost_usd == round((40_000 * 1.25 + 1_000 * 10.0) / 1_000_000, 6)


def test_gemini_quotes_model_in_url(monkeypatch):
    # The model is operator-configurable and lands in the URL path; injection
    # chars must be percent-encoded so they cannot add query params.
    captured = {}

    def fake(req, provider):
        captured["url"] = req.full_url
        return {
            "candidates": [{"content": {"parts": [{"text": "{}"}]}}],
            "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
        }

    monkeypatch.setattr(call_gemini, "request_json_with_retry", fake)
    call_gemini.GeminiClient(api_key="k", model="evil?key=LEAK&x=1").review("p")
    assert "evil%3Fkey%3DLEAK%26x%3D1" in captured["url"]
    assert "evil?key=LEAK" not in captured["url"]


def _gpt_ok_payload():
    return {
        "choices": [{"message": {"content": "{}"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


def test_gpt_uses_raised_read_timeout(monkeypatch):
    # gpt writes the longest reviews; the OpenAI call must use a read timeout
    # well above the 120s shared default so a slow generation doesn't fail the
    # round with a read TimeoutError (the burst of failures observed in prod).
    captured = {}

    def fake(req, provider, timeout=None):
        captured["provider"] = provider
        captured["timeout"] = timeout
        return _gpt_ok_payload()

    monkeypatch.delenv("OPENAI_READ_TIMEOUT", raising=False)
    monkeypatch.setattr(call_gpt, "request_json_with_retry", fake)
    call_gpt.GPTClient(api_key="k", model="gpt-5").review("p")
    assert captured["provider"] == "OpenAI"
    assert captured["timeout"] == call_gpt.DEFAULT_READ_TIMEOUT
    assert captured["timeout"] >= 300  # higher than the 120s shared default


def test_gpt_read_timeout_env_override(monkeypatch):
    # Operators can tune the ceiling per-repo via the secret.
    captured = {}

    def fake(req, provider, timeout=None):
        captured["timeout"] = timeout
        return _gpt_ok_payload()

    monkeypatch.setenv("OPENAI_READ_TIMEOUT", "450")
    monkeypatch.setattr(call_gpt, "request_json_with_retry", fake)
    call_gpt.GPTClient(api_key="k", model="gpt-5").review("p")
    assert captured["timeout"] == 450


def test_gpt_read_timeout_bad_env_falls_back(monkeypatch):
    # A typo'd secret must not disable the timeout (0/empty = wait forever) or
    # crash the client; it falls back to the default.
    captured = {}

    def fake(req, provider, timeout=None):
        captured["timeout"] = timeout
        return _gpt_ok_payload()

    monkeypatch.setenv("OPENAI_READ_TIMEOUT", "not-a-number")
    monkeypatch.setattr(call_gpt, "request_json_with_retry", fake)
    call_gpt.GPTClient(api_key="k", model="gpt-5").review("p")
    assert captured["timeout"] == call_gpt.DEFAULT_READ_TIMEOUT
