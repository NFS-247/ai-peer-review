"""AI client abstraction.

Defines a tiny interface so the dispatcher can call Claude, GPT, or Gemini
with the same shape. Each provider has its own module; this base lets the
orchestrator iterate over reviewers without caring about provider details.

Implementations live in call_claude.py, call_gpt.py, call_gemini.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


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


__all__ = ["AIResponse", "AIClient"]
