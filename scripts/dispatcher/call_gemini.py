"""Gemini (Google) AI client.

Uses the generateContent API. Returns AIResponse with token + cost accounting.

Pricing as of 2026: gemini-2.5-pro input ~$1.25/1M, output ~$10/1M (estimates;
tune via env if pricing changes).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from .ai_client import AIClient, AIResponse


GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
DEFAULT_MODEL = "gemini-2.5-pro"

INPUT_PRICE_PER_M = 1.25
OUTPUT_PRICE_PER_M = 10.00


class GeminiClient(AIClient):
    reviewer_name = "gemini"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        if not api_key:
            raise ValueError("GEMINI_API_KEY is required")
        self._api_key = api_key
        self._model = model

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
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise RuntimeError(
                f"Gemini API HTTP {exc.code}: {detail[:500]}"
            ) from exc

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


__all__ = ["GeminiClient", "GEMINI_API_BASE", "DEFAULT_MODEL"]
