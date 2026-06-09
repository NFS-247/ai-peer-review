"""Claude (Anthropic) AI client.

Uses the Messages API. Returns AIResponse with token + cost accounting.

Pricing as of 2026: claude-opus-4-7 input ~$15/1M, output ~$75/1M.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from .ai_client import AIClient, AIResponse


ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-opus-4-7"

INPUT_PRICE_PER_M = 15.00
OUTPUT_PRICE_PER_M = 75.00


class ClaudeClient(AIClient):
    reviewer_name = "claude"

    def __init__(self, api_key: str, model: str = DEFAULT_MODEL) -> None:
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is required")
        self._api_key = api_key
        self._model = model

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
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            raise RuntimeError(
                f"Anthropic API HTTP {exc.code}: {detail[:500]}"
            ) from exc

        text = ""
        for block in payload.get("content", []):
            if block.get("type") == "text":
                text += block.get("text", "")

        usage = payload.get("usage", {})
        in_tokens = int(usage.get("input_tokens", 0))
        out_tokens = int(usage.get("output_tokens", 0))
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


__all__ = ["ClaudeClient", "ANTHROPIC_API_URL", "DEFAULT_MODEL"]
