"""Prompt construction for AI reviewers.

Each reviewer gets the same factual inputs (PR title, body, diff, tier,
round, prior review history). Prompts are framed to elicit JSON output in
the strict schema required by verdict.parse_ai_json_response.

The prompt also gives each reviewer context about TradeWatcher's specific
bug classes and the operating contract. This is what makes the review
adversarial: reviewers are explicitly told what kinds of mistakes have
hurt this project before and asked to look for them.
"""

from __future__ import annotations

from textwrap import dedent
from typing import Sequence


COMMON_INSTRUCTIONS = dedent("""
    You are reviewing a pull request on a private research/paper-trading
    repository called TradeWatcher. The system is paper-only. It does not
    place broker orders. It does not enable live trading. It does not
    promote strategies automatically.

    Your job is adversarial review: find things that are wrong, missing,
    or risky. Do not be polite. Do not approve to be helpful. Approve only
    when you have actually verified the change is safe and correct.

    Specifically look for:
    - Lookahead bias (using future information at entry timestamp)
    - Safety flag changes (paper_only, live_orders_enabled,
      broker_order_submitted, dry_run, mode)
    - Broker or order submission code paths
    - Changes that touch the operating contract or its safety invariants
    - Stale tests against deleted helpers
    - Duplicate function definitions in the same file
    - Discovery/introspection-based callable selection (this has bitten
      this project four times)
    - Sample-fixture replay masquerading as real data
    - Promotion of gate-rejected candidates
    - PRs that bundle unrelated work (one objective per PR)

    Your response MUST be a single JSON object with this exact schema:

    {
      "verdict": "approve" | "request_changes" | "abstain",
      "reasoning": "<plain text explanation, 1-3 paragraphs>",
      "concerns": [
        {"file": "<file path>", "line": <int or null>, "issue": "<text>"}
      ]
    }

    - "approve" means you have verified the change is safe AND correct AND
      complete for its stated scope.
    - "request_changes" means you found problems. List them in "concerns".
    - "abstain" means you cannot meaningfully evaluate (e.g., the diff is
      empty or you lack context).

    Do not write anything outside the JSON object. Do not wrap it in
    markdown fences. Do not add commentary before or after.
""").strip()


def build_review_prompt(
    *,
    reviewer: str,
    pr_number: int,
    pr_title: str,
    pr_body: str,
    diff_text: str,
    tier: str,
    round_: int,
    prior_review_history: Sequence[str] = (),
    operator_note: str = "",
) -> str:
    """Build the user-facing prompt for an AI reviewer.

    ``operator_note`` carries the text from an OPERATOR INVESTIGATE/DISCUSS
    command. When present it is surfaced prominently so reviewers focus on the
    operator's specific instruction for this round (design Section 7).
    """
    history_section = ""
    if prior_review_history:
        joined = "\n\n---\n\n".join(prior_review_history)
        history_section = dedent(f"""
            Prior review history on this PR (most recent last):

            {joined}

            ---
        """).strip() + "\n\n"

    operator_section = ""
    if operator_note.strip():
        operator_section = dedent(f"""
            OPERATOR INSTRUCTION FOR THIS ROUND (treat as the primary focus):

            {operator_note.strip()}

            ---
        """).strip() + "\n\n"

    return dedent(f"""
        {COMMON_INSTRUCTIONS}

        ---

        Reviewer identity: {reviewer}
        PR number: #{pr_number}
        Tier: {tier}
        Round: {round_}

        ---

        {operator_section}PR title:
        {pr_title}

        PR description:
        {pr_body or "(no description)"}

        ---

        {history_section}Diff to review:

        {diff_text}

        ---

        Reply with a single JSON object per the schema above. No other text.
    """).strip()


__all__ = ["COMMON_INSTRUCTIONS", "build_review_prompt"]
