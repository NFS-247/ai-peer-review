"""GPT (OpenAI) AI client.

Uses the Chat Completions API. Returns AIResponse with token + cost accounting.

The model is selectable via the OPENAI_MODEL env var (default below); cost is
priced per-model in ``pricing.py`` (override the rate with
OPENAI_INPUT_PRICE_PER_M / OPENAI_OUTPUT_PRICE_PER_M). Pricing the call at the
model actually in use — not a fixed default rate — is what keeps the 24h spend
ledger accurate.
"""

from __future__ import annotations

import json
import os
import urllib.request
import uuid

from .ai_client import AIClient, AIResponse, request_json_with_retry
from .pricing import token_cost


OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5"
MODEL_ENV = "OPENAI_MODEL"

# gpt writes the longest reviews and intermittently exceeded the 120s shared read
# timeout mid-generation, failing the round with a read ``TimeoutError`` while
# claude/gemini finished. Give the OpenAI call a higher, operator-tunable read
# timeout (OPENAI_READ_TIMEOUT, seconds); combined with the one bounded
# timeout-retry in ``request_json_with_retry`` this keeps a slow-but-healthy
# generation from taking a required reviewer offline for the round.
DEFAULT_READ_TIMEOUT = 300
READ_TIMEOUT_ENV = "OPENAI_READ_TIMEOUT"


def _resolve_read_timeout() -> int:
    """Resolve the OpenAI read timeout (seconds): env override > default.

    A non-numeric or non-positive override falls back to the default, so a typo
    in the secret can never disable the timeout (0 would mean wait forever) or
    crash the client at construction.
    """
    raw = (os.environ.get(READ_TIMEOUT_ENV) or "").strip()
    if not raw:
        return DEFAULT_READ_TIMEOUT
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_READ_TIMEOUT
    return val if val > 0 else DEFAULT_READ_TIMEOUT


class GPTClient(AIClient):
    reviewer_name = "gpt"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self._api_key = api_key
        self._model = model or (os.environ.get(MODEL_ENV) or "").strip() or DEFAULT_MODEL
        self._read_timeout = _resolve_read_timeout()

    def review(self, prompt: str) -> AIResponse:
        body = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            OPENAI_API_URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                # All retries (429 + read-timeout) reuse this same Request, so a
                # per-call Idempotency-Key lets OpenAI dedupe a retried-but-
                # already-processed generation — billed once, not twice (closes
                # the double-charge window the bounded timeout-retry would open).
                "Idempotency-Key": f"air-{uuid.uuid4()}",
            },
        )
        payload = request_json_with_retry(
            req, provider="OpenAI", timeout=self._read_timeout
        )

        text = ""
        choices = payload.get("choices", [])
        if choices:
            text = choices[0].get("message", {}).get("content", "")

        usage = payload.get("usage", {})
        in_tokens = int(usage.get("prompt_tokens", 0))
        out_tokens = int(usage.get("completion_tokens", 0))
        # prompt_tokens is the TOTAL; cached_tokens is the (cheaper) cached subset
        # of it. OpenAI auto-caches repeated prefixes, so on later review rounds
        # most of the diff is a cache hit — bill it at the discounted rate.
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
        fresh = max(in_tokens - cached, 0)
        cost = token_cost(
            "gpt", self._model,
            fresh_input_tokens=fresh, cached_input_tokens=cached,
            output_tokens=out_tokens,
        )

        return AIResponse(
            raw_text=text,
            model=self._model,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_usd=cost,
        )


__all__ = [
    "GPTClient",
    "OPENAI_API_URL",
    "DEFAULT_MODEL",
    "DEFAULT_READ_TIMEOUT",
    "READ_TIMEOUT_ENV",
]
