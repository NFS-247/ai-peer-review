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

from .ai_client import AIClient, AIResponse, request_json_with_retry
from .pricing import cost_for


OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5"
MODEL_ENV = "OPENAI_MODEL"


class GPTClient(AIClient):
    reviewer_name = "gpt"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self._api_key = api_key
        self._model = model or (os.environ.get(MODEL_ENV) or "").strip() or DEFAULT_MODEL

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
            },
        )
        payload = request_json_with_retry(req, provider="OpenAI")

        text = ""
        choices = payload.get("choices", [])
        if choices:
            text = choices[0].get("message", {}).get("content", "")

        usage = payload.get("usage", {})
        in_tokens = int(usage.get("prompt_tokens", 0))
        out_tokens = int(usage.get("completion_tokens", 0))
        cost = cost_for("gpt", self._model, in_tokens, out_tokens)

        return AIResponse(
            raw_text=text,
            model=self._model,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_usd=cost,
        )


__all__ = ["GPTClient", "OPENAI_API_URL", "DEFAULT_MODEL"]
