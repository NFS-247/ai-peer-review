"""Posts an AI review as a PR comment with a structured verdict block.

The comment body has two parts:
1. The reviewer's freeform reasoning (extracted from the validated AI JSON
   response).
2. A fenced ``tradewatcher-verdict`` block constructed server-side from
   the validated fields. The AI cannot smuggle verdict fields into the
   block because the block is built by the dispatcher, not pasted from the
   AI's raw output.

See Section 12.5 of the design doc.
"""

from __future__ import annotations

from .github_api import GitHubAPI
from .verdict import (
    Verdict,
    build_verdict,
    compute_diff_sha256,
    compute_verdict_signature,
    parse_ai_json_response,
    utc_now_iso,
)


def post_ai_review(
    *,
    api: GitHubAPI,
    pr_number: int,
    reviewer: str,
    tier: str,
    round_: int,
    head_sha: str,
    diff_text: str,
    raw_ai_text: str,
    verdict_secret: str,
) -> Verdict:
    """Parse the AI's JSON, build the comment, post it, return the Verdict.

    The verdict block carries an HMAC signature keyed by ``verdict_secret`` so
    the trusted reader can distinguish dispatcher-emitted verdicts from any
    other github-actions[bot] comment. Raises ValueError if the AI's response
    is not valid JSON or doesn't match the schema. Caller is responsible for
    retry/escalation in that case.
    """
    parsed = parse_ai_json_response(raw_ai_text)
    verdict_value = parsed["verdict"]
    reasoning = parsed["reasoning"]
    concerns = parsed.get("concerns", []) or []

    diff_sha = compute_diff_sha256(diff_text)
    verdict = build_verdict(
        reviewer=reviewer,
        verdict=verdict_value,
        pr_number=pr_number,
        head_sha=head_sha,
        diff_sha256=diff_sha,
        tier=tier,
        round_=round_,
        emitted_at=utc_now_iso(),
    )
    signature = compute_verdict_signature(verdict, verdict_secret)

    concerns_section = ""
    if concerns:
        formatted = []
        for c in concerns:
            file = c.get("file", "")
            line = c.get("line")
            issue = c.get("issue", "")
            location = f"`{file}`"
            if line:
                location += f":L{line}"
            formatted.append(f"- {location} — {issue}")
        concerns_section = "\n\n**Concerns:**\n" + "\n".join(formatted)

    body = (
        f"**Review from `{reviewer}` (tier: `{tier}`, round: {round_})**\n\n"
        f"{reasoning}"
        f"{concerns_section}\n\n"
        f"{verdict.to_yaml_block(signature=signature)}\n"
    )
    api.post_comment(pr_number, body)
    return verdict


__all__ = ["post_ai_review"]
