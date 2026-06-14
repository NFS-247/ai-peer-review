"""Grok (xAI) AI client.

Uses xAI's OpenAI-compatible Chat Completions API. Returns AIResponse with
token + cost accounting.

The model is selectable via the GROK_MODEL env var (default below); cost is
priced per-model in ``pricing.py`` (override the rate with
XAI_INPUT_PRICE_PER_M / XAI_OUTPUT_PRICE_PER_M). Pricing the call at the model
actually in use — not a fixed default rate — is what keeps the 24h spend ledger
accurate.

xAI's API is OpenAI-shaped, so the request/response handling mirrors call_gpt.py.
The billing posture is the CONSERVATIVE one (like claude/gemini): a read timeout
is NOT retried (timeout_retries=0). gpt opts into a single bounded timeout retry
only because it sends an Idempotency-Key OpenAI honors for de-duplication; xAI's
support for that header isn't relied upon here, so a timeout fails the round
cleanly rather than risk a double charge.
"""

from __future__ import annotations

import json
import os
import urllib.request

from .ai_client import AIClient, AIResponse, REVIEWER_READ_TIMEOUT, request_json_with_retry
from .pricing import token_cost


GROK_API_URL = "https://api.x.ai/v1/chat/completions"
DEFAULT_MODEL = "grok-4"
MODEL_ENV = "GROK_MODEL"


class GrokClient(AIClient):
    reviewer_name = "grok"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise ValueError("XAI_API_KEY is required")
        self._api_key = api_key
        self._model = model or (os.environ.get(MODEL_ENV) or "").strip() or DEFAULT_MODEL

    def review(self, prompt: str) -> AIResponse:
        body = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            GROK_API_URL,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
        )
        # timeout_retries=0 is explicit, not just the default: there's no
        # idempotency key here, so retrying a read timeout could double-bill if
        # the model already produced the (lost) reply.
        payload = request_json_with_retry(
            req,
            provider="xAI",
            timeout=REVIEWER_READ_TIMEOUT,
            timeout_retries=0,
        )

        text = ""
        choices = payload.get("choices", [])
        if choices:
            text = choices[0].get("message", {}).get("content", "")

        usage = payload.get("usage", {})
        in_tokens = int(usage.get("prompt_tokens", 0))
        out_tokens = int(usage.get("completion_tokens", 0))
        # OpenAI-shaped usage: prompt_tokens is the TOTAL; cached_tokens (when the
        # provider reports it) is the cheaper cache-hit subset. xAI may omit it,
        # in which case cached is 0 and this is exactly tokens × rate.
        cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
        fresh = max(in_tokens - cached, 0)
        cost = token_cost(
            "grok", self._model,
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


__all__ = ["GrokClient", "GROK_API_URL", "DEFAULT_MODEL", "MODEL_ENV"]
