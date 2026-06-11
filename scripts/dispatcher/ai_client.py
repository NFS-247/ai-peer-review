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


def _is_timeout_error(exc: BaseException) -> bool:
    """True if ``exc`` is a read/connect timeout — bare or URLError-wrapped.

    A timeout surfaces as ``socket.timeout`` (an alias of ``TimeoutError``) or,
    from inside urllib's opener, as a ``URLError`` whose ``reason`` is that
    timeout (or, on some stacks, the literal string "timed out"). Both must route
    to the timeout budget rather than the transient (429/URLError) one.
    """
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return True
    return isinstance(reason, str) and "timed out" in reason.lower()


def request_json_with_retry(
    req: urllib.request.Request,
    *,
    provider: str,
    timeout: int = 120,
    max_attempts: int = 4,
    base_delay: float = 2.0,
    max_delay: float = 60.0,
    timeout_retries: int = 0,
    timeout_backoff: float = 5.0,
    sleep=time.sleep,
) -> dict:
    """POST ``req`` and parse the JSON response, retrying transient failures.

    Retries on HTTP 429 / 5xx and on network errors with exponential backoff
    (honoring a ``Retry-After`` header when present), so a momentary rate limit
    no longer fails a whole review round. After ``max_attempts`` it raises
    ``RuntimeError`` with the provider + status, which the orchestrator treats
    as a reviewer being unavailable (and escalates).

    A *read*/connect timeout is handled on its own budget. It surfaces as
    ``socket.timeout`` (an alias of ``TimeoutError``) or, from inside urllib's
    opener, as a ``URLError`` wrapping one; both are routed to the timeout path
    rather than the transient (429/URLError) path. Without that, a timeout fails
    the round outright (how gpt, the longest reviews, intermittently lost rounds
    while claude/gemini finished) or silently eats the transient budget.

    ``timeout_retries`` defaults to 0 — a timeout fails the round cleanly with no
    extra attempt — because retrying a timeout can double-bill if the request
    already completed server-side. A caller that is idempotency-protected opts
    in: the OpenAI client sets ``timeout_retries=1`` and sends an
    ``Idempotency-Key`` so OpenAI can de-duplicate a retried request it already
    processed, making a double-charge unlikely (not impossible), so the retry
    stays small and bounded.

    The timeout budget is independent from the ``max_attempts`` (429/URLError)
    budget: a timeout consumes a ``timeout_retries`` slot, never an ``attempt``
    or its backoff index, so a slow generation can't quietly eat the
    transient-failure budget.

    Every wait is capped at ``max_delay`` seconds — including a server-sent
    ``Retry-After`` — so a buggy or hostile upstream (e.g. ``Retry-After:
    86400``) can never stall the job; the round fails fast and escalates.
    """
    # Two independent budgets: ``attempt`` covers retryable HTTP (429/503) and
    # generic URLError transient failures; ``timeouts_seen`` covers read/connect
    # timeouts. A timeout never touches ``attempt`` (nor its backoff index), so a
    # slow generation can't quietly eat the 429/URLError budget. The loop is
    # bounded by the sum of both budgets — a hard backstop so it can never spin
    # even if a future branch grows an ungated ``continue``; in normal operation
    # a branch always raises or hits its own budget first.
    attempt = 0
    timeouts_seen = 0
    for _ in range(max_attempts + timeout_retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            # Only a retryable status consumes the transient budget; a 400/401/etc
            # raises at once without touching ``attempt`` (which then means exactly
            # "retryable attempts made").
            retryable = exc.code in RETRYABLE_STATUS
            if retryable:
                attempt += 1
            if retryable and attempt < max_attempts:
                delay = _retry_after_seconds(exc)
                if delay is None:
                    delay = base_delay * (2 ** (attempt - 1))
                sleep(min(delay, max_delay))
                continue
            raise RuntimeError(
                f"{provider} API HTTP {exc.code}: {detail[:500]}"
            ) from exc
        except (socket.timeout, TimeoutError, urllib.error.URLError) as exc:
            # A read/connect timeout arrives bare (socket.timeout/TimeoutError) or
            # wrapped as URLError(reason=timeout); both use the SEPARATE timeout
            # budget. Any other URLError is a generic transient failure.
            if _is_timeout_error(exc):
                if timeouts_seen < timeout_retries:
                    timeouts_seen += 1
                    sleep(min(timeout_backoff, max_delay))
                    continue
                window = f" after {timeout}s" if timeout is not None else ""
                raise RuntimeError(
                    f"{provider} API read timed out{window} "
                    f"(retried {timeouts_seen}x)"
                ) from exc
            attempt += 1
            if attempt < max_attempts:
                sleep(min(base_delay * (2 ** (attempt - 1)), max_delay))
                continue
            raise RuntimeError(
                f"{provider} API request failed: {exc.reason}"
            ) from exc
    raise RuntimeError(
        f"{provider} API: retries exhausted "
        f"({attempt} transient attempts, {timeouts_seen} timeouts)"
    )


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
