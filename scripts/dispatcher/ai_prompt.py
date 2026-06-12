"""Prompt construction for AI reviewers.

Each reviewer gets the same factual inputs (PR title, body, diff, tier, round,
prior review history) plus project context supplied by the consuming repo's
config: ``project_description`` (what this repo is) and ``review_guidance``
(the bug classes that have hurt it before). The base instructions are
project-agnostic; a repo injects its own domain-specific concerns via its
.peer-review.json rather than the dispatcher hard-coding any one project's
risk model. This is what keeps review adversarial AND multi-tenant.
"""

from __future__ import annotations

from textwrap import dedent
from typing import Sequence


# Project-agnostic adversarial-review instructions. The project framing (what
# this repo is) and project-specific bug classes (what has hurt it before) are
# injected from config — see build_common_instructions.
_BASE_INSTRUCTIONS_HEAD = dedent("""
    You are reviewing a pull request.{project_line}

    Your job is adversarial review: find things that are wrong, missing,
    or risky. Do not be polite. Do not approve to be helpful. Approve only
    when you have actually verified the change is safe and correct.

    Always look for:
    - Bugs, logic errors, and unhandled edge cases
    - Security problems (injection, secret/credential leakage, auth/authz)
    - Changes that touch safety-critical, irreversible, or money-moving behavior
    - Stale tests against deleted helpers; tests that don't exercise the change
    - Duplicate or dead code
    - PRs that bundle unrelated work (one objective per PR)

    Calibrate your concerns to THIS project and THIS change:
    - Prefer the project's stated conventions and the operator's standing
      decisions (shown below, if any) over generic best practices. A practice
      that is good "in general" can be the wrong call for this repo — the
      operator owns that judgment, not you.
    - Raise a concern only when it is a concrete defect in this diff, not a
      general "you could also add X." If you mention such a suggestion at all,
      keep it out of "concerns" unless it's an actual problem here.
    - If you reviewed an earlier round, do NOT re-raise a concern the new diff
      already resolved, or one the operator has explicitly overruled. Read the
      prior review history below before repeating yourself.
""").strip()

_SCHEMA_TAIL = dedent("""
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


def build_common_instructions(
    *, project_description: str = "", review_guidance: Sequence[str] = ()
) -> str:
    """Assemble reviewer instructions from the generic base + project context.

    ``project_description`` is one or two sentences about what the repo is
    (e.g. "a paper-only trading system; it never places broker orders").
    ``review_guidance`` is a list of project-specific bug classes to hunt for.
    Both default empty, yielding fully generic instructions.
    """
    project_line = ""
    if project_description.strip():
        project_line = " " + project_description.strip()
    head = _BASE_INSTRUCTIONS_HEAD.format(project_line=project_line)

    guidance_section = ""
    if review_guidance:
        bullets = "\n".join(f"    - {g}" for g in review_guidance)
        guidance_section = (
            "\n\n    Specifically for THIS project, also look for:\n" + bullets
        )

    return f"{head}{guidance_section}\n\n{_SCHEMA_TAIL}"


# Generic instructions with no project context. Kept as a module constant so
# existing imports of COMMON_INSTRUCTIONS still resolve.
COMMON_INSTRUCTIONS = build_common_instructions()


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
    operator_decisions: Sequence[str] = (),
    project_description: str = "",
    review_guidance: Sequence[str] = (),
) -> str:
    """Build the user-facing prompt for an AI reviewer.

    ``operator_note`` carries the text from an OPERATOR INVESTIGATE/DISCUSS
    command. When present it is surfaced prominently so reviewers focus on the
    operator's specific instruction for this round (design Section 7).
    ``prior_review_history`` is the panel's earlier reviews on this PR (one
    block per reviewer, their latest) so a reviewer has memory across rounds and
    stops re-raising resolved concerns. ``operator_decisions`` are the owner's
    STANDING rulings on this PR (OPERATOR APPROVE/INVESTIGATE/...): reviewers
    honor them and do not relitigate. ``project_description`` / ``review_guidance``
    inject the consuming repo's domain context (from its .peer-review.json) so
    reviews are tailored per tenant instead of hard-coded to one project.
    """
    common = build_common_instructions(
        project_description=project_description,
        review_guidance=review_guidance,
    )

    decisions_section = ""
    if operator_decisions:
        decisions = "\n\n".join(d.strip() for d in operator_decisions if d.strip())
        if decisions:
            decisions_section = dedent(f"""
                Standing operator decisions on this PR (the OWNER has ruled —
                honor these and do NOT relitigate them):

                {decisions}

                ---
            """).strip() + "\n\n"

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
        {common}

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

        {decisions_section}{history_section}Diff to review:

        {diff_text}

        ---

        Reply with a single JSON object per the schema above. No other text.
    """).strip()


__all__ = ["COMMON_INSTRUCTIONS", "build_common_instructions", "build_review_prompt"]
