"""AI client abstraction.

Defines a tiny interface so the dispatcher can call Claude, GPT, or Gemini
with the same shape. Each provider has its own module; this base lets the
orchestrator iterate over reviewers without caring about provider details.

Implementations live in call_claude.py, call_gpt.py, call_gemini.py.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


# Transient HTTP statuses worth retrying: rate limit + transient server errors.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _retry_after_seconds(exc: urllib.error.HTTPError) -> Optional[float]:
    """Parse a Retry-After header (seconds form) if the provider sent one."""
    try:
        val = exc.headers.get("Retry-After") if exc.headers else None
    except Exception:  # noqa: BLE001 - header access must never crash the call
        return None
    if not val:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def request_json_with_retry(
    req: urllib.request.Request,
    *,
    provider: str,
    timeout: int = 120,
    max_attempts: int = 4,
    base_delay: float = 2.0,
    sleep=time.sleep,
) -> dict:
    """POST ``req`` and parse the JSON response, retrying transient failures.

    Retries on HTTP 429 / 5xx and on network errors with exponential backoff
    (honoring a ``Retry-After`` header when present), so a momentary rate limit
    no longer fails a whole review round. After ``max_attempts`` it raises
    ``RuntimeError`` with the provider + status, which the orchestrator treats
    as a reviewer being unavailable (and escalates).
    """
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            if exc.code in RETRYABLE_STATUS and attempt < max_attempts:
                delay = _retry_after_seconds(exc)
                if delay is None:
                    delay = base_delay * (2 ** (attempt - 1))
                sleep(delay)
                continue
            raise RuntimeError(
                f"{provider} API HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            if attempt < max_attempts:
                sleep(base_delay * (2 ** (attempt - 1)))
                continue
            raise RuntimeError(
                f"{provider} API request failed: {exc.reason}"
            ) from exc
    raise RuntimeError(f"{provider} API: exhausted {max_attempts} attempts")


@dataclass(frozen=True)
class AIResponse:
    """Raw response from an AI client."""

    raw_text: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


class AIClient(ABC):
    """Abstract AI reviewer."""

    reviewer_name: str = ""  # "claude", "gpt", "gemini"

    @abstractmethod
    def review(self, prompt: str) -> AIResponse:
        """Send the prompt, get the raw response. Raises on transport error."""
        ...


__all__ = ["AIResponse", "AIClient", "request_json_with_retry", "RETRYABLE_STATUS"]
