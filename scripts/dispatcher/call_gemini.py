"""Gemini (Google) AI client.

Uses the generateContent API. Returns AIResponse with token + cost accounting.

The model is selectable via the GEMINI_MODEL env var (default below); cost is
priced per-model in ``pricing.py`` (override the rate with
GEMINI_INPUT_PRICE_PER_M / GEMINI_OUTPUT_PRICE_PER_M). Pricing the call at the
model actually in use — not a fixed default rate — is what keeps the 24h spend
ledger accurate.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from .ai_client import AIClient, AIResponse, request_json_with_retry
from .pricing import cost_for


GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-2.5-pro"
MODEL_ENV = "GEMINI_MODEL"


class GeminiClient(AIClient):
    reviewer_name = "gemini"

    def __init__(self, api_key: str, model: str | None = None) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required")
        self._api_key = api_key
        self._model = model or (os.environ.get(MODEL_ENV) or "").strip() or DEFAULT_MODEL

    def review(self, prompt: str) -> AIResponse:
        url = (
            f"{GEMINI_API_BASE}/{self._model}:generateContent"
            f"?key={urllib.parse.quote(self._api_key)}"
        )
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseMimeType": "application/json",
            },
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        payload = request_json_with_retry(req, provider="Gemini")

        text = ""
        candidates = payload.get("candidates", [])
        if candidates:
            parts = candidates[0].get("content", {}).get("parts", [])
            for p in parts:
                if "text" in p:
                    text += p["text"]

        usage = payload.get("usageMetadata", {})
        in_tokens = int(usage.get("promptTokenCount", 0))
        out_tokens = int(usage.get("candidatesTokenCount", 0))
        cost = cost_for("gemini", self._model, in_tokens, out_tokens)

        return AIResponse(
            raw_text=text,
            model=self._model,
            input_tokens=in_tokens,
            output_tokens=out_tokens,
            cost_usd=cost,
        )


__all__ = ["GeminiClient", "GEMINI_API_BASE", "DEFAULT_MODEL"]
