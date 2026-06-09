"""Structured verdict format for the AI peer review dispatcher.

Implements Section 12.5 of the design doc. A verdict is the only thing that
counts toward convergence. Freeform PR comments, GitHub's native "Approve"
review submissions, and "lgtm" text in any comment do NOT count.

A verdict is emitted by the dispatcher itself (not copied from raw AI output)
based on the AI's validated JSON response. The verdict is embedded in a PR
comment as a fenced code block tagged ``tradewatcher-verdict``.

This module is pure: parse takes text, returns a Verdict or None; build takes
a Verdict, returns text.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional


VERDICT_APPROVE = "approve"
VERDICT_REQUEST_CHANGES = "request_changes"
VERDICT_ABSTAIN = "abstain"

VALID_VERDICTS = frozenset({VERDICT_APPROVE, VERDICT_REQUEST_CHANGES, VERDICT_ABSTAIN})

VALID_REVIEWERS = frozenset({"claude", "gpt", "gemini"})

VERDICT_FENCE_OPEN = "```tradewatcher-verdict"
VERDICT_FENCE_CLOSE = "```"

# Regex to extract a verdict block from a comment body. Multiline, non-greedy.
_VERDICT_BLOCK_RE = re.compile(
    r"```tradewatcher-verdict\s*\n(.*?)\n```",
    re.DOTALL,
)


@dataclass(frozen=True)
class Verdict:
    """A structured verdict record. Immutable.

    Equality is field-based (frozen dataclass).
    """

    reviewer: str
    verdict: str
    pr_number: int
    head_sha: str
    diff_sha256: str
    tier: str
    round: int
    emitted_at: str  # ISO 8601 UTC

    def to_dict(self) -> dict:
        return {
            "reviewer": self.reviewer,
            "verdict": self.verdict,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "diff_sha256": self.diff_sha256,
            "tier": self.tier,
            "round": self.round,
            "emitted_at": self.emitted_at,
        }

    def canonical_signing_string(self) -> str:
        """Stable string over which the HMAC signature is computed.

        Order-independent: built from the sorted JSON of the verdict fields.
        Any change to any field changes the signature.
        """
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def to_yaml_block(self, signature: Optional[str] = None) -> str:
        """Serialize as the verdict fence block.

        If ``signature`` is provided it is included as a ``signature`` line.
        The trusted reader requires a valid signature to count the verdict
        (see Section 12.5 hardening: only dispatcher-emitted, HMAC-signed
        verdicts count).
        """
        lines = [
            VERDICT_FENCE_OPEN,
            f"reviewer: {self.reviewer}",
            f"verdict: {self.verdict}",
            f"pr_number: {self.pr_number}",
            f"head_sha: {self.head_sha}",
            f"diff_sha256: {self.diff_sha256}",
            f"tier: {self.tier}",
            f"round: {self.round}",
            f"emitted_at: {self.emitted_at}",
        ]
        if signature is not None:
            lines.append(f"signature: {signature}")
        lines.append(VERDICT_FENCE_CLOSE)
        return "\n".join(lines)


def compute_verdict_signature(verdict: "Verdict", secret: str) -> str:
    """HMAC-SHA256 signature of a verdict, keyed by the dispatcher secret.

    Only the dispatcher (which holds DISPATCHER_VERDICT_SECRET) can produce a
    valid signature. Another GitHub Action posting as github-actions[bot]
    cannot forge a verdict without the secret. Adding a secret-reading
    workflow is itself a high-stakes, reviewed change.
    """
    return hmac.new(
        secret.encode("utf-8"),
        verdict.canonical_signing_string().encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def verify_verdict_signature(verdict: "Verdict", signature: Optional[str], secret: str) -> bool:
    """Constant-time check that ``signature`` is a valid HMAC for ``verdict``."""
    if not signature or not secret:
        return False
    expected = compute_verdict_signature(verdict, secret)
    return hmac.compare_digest(expected, signature)


def compute_diff_sha256(diff_text: str) -> str:
    """Compute the SHA-256 of a diff string."""
    return hashlib.sha256(diff_text.encode("utf-8")).hexdigest()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_verdict(
    *,
    reviewer: str,
    verdict: str,
    pr_number: int,
    head_sha: str,
    diff_sha256: str,
    tier: str,
    round_: int,
    emitted_at: Optional[str] = None,
) -> Verdict:
    """Construct a Verdict, validating each field.

    Raises ValueError if any field is invalid.
    """
    if reviewer not in VALID_REVIEWERS:
        raise ValueError(f"reviewer must be one of {VALID_REVIEWERS}, got {reviewer!r}")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {VALID_VERDICTS}, got {verdict!r}")
    if not isinstance(pr_number, int) or pr_number <= 0:
        raise ValueError(f"pr_number must be a positive int, got {pr_number!r}")
    if not isinstance(head_sha, str) or len(head_sha) < 7:
        raise ValueError(f"head_sha must be a non-trivial string, got {head_sha!r}")
    if not isinstance(diff_sha256, str) or len(diff_sha256) != 64:
        raise ValueError(
            f"diff_sha256 must be a 64-char hex string, got {diff_sha256!r}"
        )
    if not isinstance(tier, str) or not tier:
        raise ValueError(f"tier must be a non-empty string, got {tier!r}")
    if not isinstance(round_, int) or round_ < 1:
        raise ValueError(f"round must be a positive int, got {round_!r}")

    return Verdict(
        reviewer=reviewer,
        verdict=verdict,
        pr_number=pr_number,
        head_sha=head_sha,
        diff_sha256=diff_sha256,
        tier=tier,
        round=round_,
        emitted_at=emitted_at or utc_now_iso(),
    )


def _parse_block_fields(body: str) -> Optional[dict[str, str]]:
    if not body:
        return None
    matches = _VERDICT_BLOCK_RE.findall(body)
    if not matches:
        return None
    # If multiple blocks exist, the LAST one wins.
    block_body = matches[-1]
    fields: dict[str, str] = {}
    for line in block_body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            return None
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def parse_verdict_from_comment(body: str) -> Optional[Verdict]:
    """Extract a Verdict from a comment body, if present and structurally valid.

    Does NOT verify the HMAC signature — that is the trusted reader's job
    (see ``parse_signed_verdict_from_comment``). Returns None on any malformed
    field. The verdict format is strict by design.
    """
    fields = _parse_block_fields(body)
    if fields is None:
        return None

    required = {
        "reviewer", "verdict", "pr_number", "head_sha",
        "diff_sha256", "tier", "round", "emitted_at",
    }
    if not required.issubset(fields.keys()):
        return None

    try:
        pr_number = int(fields["pr_number"])
        round_ = int(fields["round"])
    except (ValueError, KeyError):
        return None

    try:
        return build_verdict(
            reviewer=fields["reviewer"],
            verdict=fields["verdict"],
            pr_number=pr_number,
            head_sha=fields["head_sha"],
            diff_sha256=fields["diff_sha256"],
            tier=fields["tier"],
            round_=round_,
            emitted_at=fields["emitted_at"],
        )
    except ValueError:
        return None


def parse_signed_verdict_from_comment(
    body: str, secret: str
) -> Optional[Verdict]:
    """Extract a Verdict ONLY if it carries a valid dispatcher HMAC signature.

    This is the trusted reader used for convergence. A comment without a
    ``signature`` line, or with an invalid signature, returns None — even if
    it is otherwise a perfectly-formed verdict block authored by
    github-actions[bot]. This closes the "another Actions workflow forges a
    verdict" hole, because forging requires DISPATCHER_VERDICT_SECRET.
    """
    verdict = parse_verdict_from_comment(body)
    if verdict is None:
        return None
    fields = _parse_block_fields(body) or {}
    signature = fields.get("signature")
    if not verify_verdict_signature(verdict, signature, secret):
        return None
    return verdict


def parse_ai_json_response(raw_json: str) -> dict:
    """Parse and validate an AI's JSON response.

    The AI is asked to return JSON with the shape:
        {
          "verdict": "approve" | "request_changes" | "abstain",
          "reasoning": "<free text>",
          "concerns": [{"file": "...", "line": N, "issue": "..."}, ...]
        }

    Returns the parsed dict, raising ValueError on any schema violation.
    The dispatcher constructs the verdict block server-side from the validated
    "verdict" field — the AI cannot smuggle a fake verdict.
    """
    try:
        data = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("AI response must be a JSON object")

    if "verdict" not in data:
        raise ValueError("AI response missing 'verdict' field")

    if data["verdict"] not in VALID_VERDICTS:
        raise ValueError(
            f"AI verdict must be one of {sorted(VALID_VERDICTS)}, "
            f"got {data['verdict']!r}"
        )

    if "reasoning" not in data or not isinstance(data["reasoning"], str):
        raise ValueError("AI response missing 'reasoning' string field")

    if "concerns" in data and not isinstance(data["concerns"], list):
        raise ValueError("AI 'concerns' must be a list if present")

    return data


__all__ = [
    "VERDICT_APPROVE",
    "VERDICT_REQUEST_CHANGES",
    "VERDICT_ABSTAIN",
    "VALID_VERDICTS",
    "VALID_REVIEWERS",
    "Verdict",
    "compute_diff_sha256",
    "compute_verdict_signature",
    "verify_verdict_signature",
    "utc_now_iso",
    "build_verdict",
    "parse_verdict_from_comment",
    "parse_signed_verdict_from_comment",
    "parse_ai_json_response",
]
