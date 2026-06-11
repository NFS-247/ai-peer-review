"""AI client abstraction.

Defines a tiny interface so the dispatcher can call Claude, GPT, or Gemini
with the same shape. Each provider has its own module; this base lets the
orchestrator iterate over reviewers without caring about provider details.

Implementations live in call_claude.py, call_gpt.py, call_gemini.py.
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


# Statuses safe to retry WITHOUT idempotency keys: the request was rejected
# before the provider processed (and billed) it. 429 = rate limited, 503 =
# service unavailable. We deliberately do NOT retry 500/502/504 — those may have
# been partially processed, so a blind retry could double-charge.
RETRYABLE_STATUS = frozenset({429, 503})


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
    max_delay: float = 60.0,
    timeout_retries: int = 1,
    timeout_backoff: float = 5.0,
    sleep=time.sleep,
) -> dict:
    """POST ``req`` and parse the JSON response, retrying transient failures.

    Retries on HTTP 429 / 5xx and on network errors with exponential backoff
    (honoring a ``Retry-After`` header when present), so a momentary rate limit
    no longer fails a whole review round. After ``max_attempts`` it raises
    ``RuntimeError`` with the provider + status, which the orchestrator treats
    as a reviewer being unavailable (and escalates).

    A *read* timeout is handled on its own budget. ``urlopen`` raises
    ``socket.timeout`` (an alias of ``TimeoutError``) when a slow generation
    exceeds ``timeout``; that is NOT a ``URLError``, so without this branch it
    would fail the round outright — exactly how gpt (the longest reviews)
    intermittently lost rounds while claude/gemini finished. It gets up to
    ``timeout_retries`` extra attempt(s) after a short ``timeout_backoff`` before
    the reviewer is declared unavailable. As with the 5xx case the request may
    already be in flight server-side, so this retry is deliberately small and
    bounded: a rare double-charge is the accepted cost of not losing a required
    reviewer to one slow round.

    Every wait is capped at ``max_delay`` seconds — including a server-sent
    ``Retry-After`` — so a buggy or hostile upstream (e.g. ``Retry-After:
    86400``) can never stall the job; the round fails fast and escalates.
    """
    timeouts_seen = 0
    attempt = 0
    while True:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            if exc.code in RETRYABLE_STATUS and attempt < max_attempts:
                delay = _retry_after_seconds(exc)
                if delay is None:
                    delay = base_delay * (2 ** (attempt - 1))
                sleep(min(delay, max_delay))
                continue
            raise RuntimeError(
                f"{provider} API HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            # A read timeout is a sibling of OSError, not a URLError, so it must
            # be caught explicitly or it escapes the retry loop entirely.
            if timeouts_seen < timeout_retries:
                timeouts_seen += 1
                sleep(min(timeout_backoff, max_delay))
                continue
            raise RuntimeError(
                f"{provider} API read timed out after {timeout}s "
                f"(retried {timeouts_seen}x)"
            ) from exc
        except urllib.error.URLError as exc:
            if attempt < max_attempts:
                sleep(min(base_delay * (2 ** (attempt - 1)), max_delay))
                continue
            raise RuntimeError(
                f"{provider} API request failed: {exc.reason}"
            ) from exc


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
