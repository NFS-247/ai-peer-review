"""GPT (OpenAI) AI client.

Uses the Chat Completions API. Returns AIResponse with token + cost accounting.

Pricing as of 2026: gpt-5-class input ~$5/1M, output ~$20/1M (estimates;
tune via env if pricing changes).
"""

from __future__ import annotations

import json
import urllib.request

from .ai_client import AIClient, AIResponse, request_json_with_retry


OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-5"

INPUT_PRICE_PER_M = 5.00
OUTPUT_PRICE_PER_M = 20.00


class GPTClient(AIClient):
    reviewer_name = "gpt"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        if not api_key:
            raise ValueError("OPENAI_API_KEY is required")
        self._api_key = api_key
        self._model = model

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
        cost = (in_tokens * INPUT_PRICE_PER_M / 1_000_000) + (
            out_tokens * OUTPUT_PRICE_PER_M / 1_000_000
        )

        return AIResponse(
            raw_text=text,
            model=self._model,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_usd=round(cost, 6),
        )


__all__ = ["GPTClient", "OPENAI_API_URL", "DEFAULT_MODEL"]
