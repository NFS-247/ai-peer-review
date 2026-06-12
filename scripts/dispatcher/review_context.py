"""Reconstruct reviewer 'memory' from a PR's own history.

The AI reviewers are stateless single-shot calls: each round they see the diff
cold. With no recall of their prior reviews — or of the operator's standing
decisions — they re-raise the same concerns every round, and relitigate points
the owner has already ruled on. These pure helpers rebuild that context from the
PR's existing comments so ``build_review_prompt`` can feed it back in.

Trust model (deliberately the SAME gate convergence uses):
  - A "prior review" counts only if the comment carries a VALID dispatcher
    HMAC-signed verdict block — so a PR author can't inject fake prior reviews
    into the prompt, and it works no matter which bot login posted it.
  - A "standing operator decision" counts only from the configured operator's
    login (GitHub authenticates comment authorship, so it can't be spoofed).

Pure / stdlib-only: functions take plain comment data and return prompt-ready
strings. No GitHub calls here.
"""

from __future__ import annotations

from typing import Iterable

from .parse_reply import OPERATOR_PREFIX
from .verdict import VERDICT_FENCE_OPEN, parse_signed_verdict_from_comment


# Operator verbs that carry a STANDING ruling reviewers should honor (an owner
# decision on the substance). Lifecycle verbs (PAUSE/RESUME/KILL) are not review
# context and are skipped.
_DECISION_VERBS = frozenset({"APPROVE", "INVESTIGATE", "DISCUSS", "BLOCK"})

# Safety cap on each block's length so one pathological review/note can't blow up
# the prompt (and the token bill). Reviews are normally 1-3 short paragraphs.
_MAX_BLOCK_CHARS = 2000


def _strip_verdict_block(body: str) -> str:
    """Drop the trailing signed verdict fence — internal plumbing, not for the model."""
    idx = body.find(VERDICT_FENCE_OPEN)
    return (body[:idx] if idx != -1 else body).rstrip()


def summarize_prior_reviews(
    comment_bodies: Iterable[str], *, secret: str, max_block_chars: int = _MAX_BLOCK_CHARS
) -> list[str]:
    """Each reviewer's MOST RECENT signed review, verdict fence stripped, reviewer-sorted.

    ``comment_bodies`` is the PR's comment bodies in order (oldest->newest). A body
    is trusted only if it parses as a validly-signed dispatcher verdict (so this
    is exactly what convergence trusts). For each reviewer the latest round wins,
    so the panel sees at most one block per reviewer — bounded context, highest
    signal: their current standing position and concerns. Returns ``[]`` when the
    secret is empty (fail-closed, same as convergence).
    """
    if not secret:
        return []
    latest: dict[str, tuple[int, str]] = {}
    for body in comment_bodies:
        verdict = parse_signed_verdict_from_comment(body or "", secret)
        if verdict is None:
            continue
        prev = latest.get(verdict.reviewer)
        if prev is None or verdict.round >= prev[0]:
            text = _strip_verdict_block(body).strip()[:max_block_chars]
            latest[verdict.reviewer] = (verdict.round, text)
    return [latest[r][1] for r in sorted(latest)]


def summarize_operator_decisions(
    comments: Iterable[tuple[str, str]],
    *,
    operator_login: str,
    max_block_chars: int = _MAX_BLOCK_CHARS,
) -> list[str]:
    """The operator's standing OPERATOR rulings on this PR, in order (oldest->newest).

    ``comments`` is an ordered iterable of ``(author_login, body)``. Only the
    configured operator's comments count (GitHub guarantees comment authorship,
    so the login can't be forged), and only substantive verbs
    (APPROVE/INVESTIGATE/DISCUSS/BLOCK) — these are decisions reviewers must not
    relitigate. Lifecycle verbs (PAUSE/RESUME/KILL) are skipped. Each entry is the
    operator's comment, trimmed and length-capped. Returns ``[]`` when no operator
    login is configured (can't attribute a decision safely).
    """
    if not operator_login:
        return []
    out: list[str] = []
    for author, body in comments:
        if author != operator_login or not body:
            continue
        first = next((ln for ln in body.splitlines() if ln.strip()), "")
        if not first.startswith(OPERATOR_PREFIX):
            continue
        rest = first[len(OPERATOR_PREFIX):].strip()
        verb = rest.split(maxsplit=1)[0].upper() if rest else ""
        if verb in _DECISION_VERBS:
            out.append(body.strip()[:max_block_chars])
    return out


__all__ = ["summarize_prior_reviews", "summarize_operator_decisions"]
