"""Claude (Anthropic) AI client.

Uses the Messages API. Returns AIResponse with token + cost accounting.

The model is selectable via the ANTHROPIC_MODEL env var (default below); cost is
priced per-model in ``pricing.py`` (override the rate with
ANTHROPIC_INPUT_PRICE_PER_M / ANTHROPIC_OUTPUT_PRICE_PER_M). Pricing the call at
the model actually in use — not a fixed default rate — is what keeps the 24h
spend ledger accurate.
"""

from __future__ import annotations

import json
import os
import urllib.request

from .ai_client import AIClient, AIResponse, REVIEWER_READ_TIMEOUT, request_json_with_retry
from .pricing import token_cost


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-opus-4-7"
MODEL_ENV = "ANTHROPIC_MODEL"


class ClaudeClient(AIClient):
    reviewer_name = "claude"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required")
        self._api_key = api_key
        self._model = model or (os.environ.get(MODEL_ENV) or "").strip() or DEFAULT_MODEL

    def review(self, prompt: str) -> AIResponse:
        body = {
            "model": self._model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        req = urllib.request.Request(
            ANTHROPIC_API_URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        # timeout_retries=0 is explicit, not just the default: a reviewer read
        # timeout must never be retried — there's no idempotency key here, so a
        # retry could double-bill if the model already produced the (lost) reply.
        payload = request_json_with_retry(
            req,
            provider="Anthropic",
            timeout=REVIEWER_READ_TIMEOUT,
            timeout_retries=0,
        )

        text = ""
        for block in payload.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        usage = payload.get("usage", {})
        # Anthropic's input_tokens EXCLUDES cache reads/writes, which are reported
        # separately (cache reads ~0.1x, cache writes ~1.25x the input rate).
        # Accounted for here so the cost is right if prompt caching is ever
        # enabled; both are 0 today (no cache_control set), so cost is unchanged.
        in_tokens = int(usage.get("input_tokens", 0))
        out_tokens = int(usage.get("output_tokens", 0))
        cache_read = int(usage.get("cache_read_input_tokens", 0))
        cache_write = int(usage.get("cache_creation_input_tokens", 0))
        cost = token_cost(
            "claude", self._model,
            fresh_input_tokens=in_tokens, cached_input_tokens=cache_read,
            cache_write_tokens=cache_write, output_tokens=out_tokens,
        )

        return AIResponse(
            raw_text=text,
            model=self._model,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_usd=cost,
        )


__all__ = ["ClaudeClient", "ANTHROPIC_API_URL", "DEFAULT_MODEL"]
