"""Operator command parser for the AI peer review dispatcher.

Implements Section 7 of the design doc. Only PR comments that:
  (a) start with the literal prefix "OPERATOR " (uppercase, trailing space)
  (b) are authored by the configured operator account (verified via user.id)
  (c) contain a recognized verb as the first word after the prefix
are acted on. Anything else gets either a no-op reply (if the prefix is
present but the verb is invalid) or no action (if the prefix is missing).

This module is pure: parse takes text + author info, returns a Command or
the appropriate error. No I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


OPERATOR_PREFIX = "OPERATOR "

CMD_APPROVE = "APPROVE"
CMD_BLOCK = "BLOCK"
CMD_INVESTIGATE = "INVESTIGATE"
CMD_DISCUSS = "DISCUSS"
CMD_PAUSE = "PAUSE"
CMD_RESUME = "RESUME"
CMD_KILL = "KILL"

VALID_VERBS = frozenset({
    CMD_APPROVE,
    CMD_BLOCK,
    CMD_INVESTIGATE,
    CMD_DISCUSS,
    CMD_PAUSE,
    CMD_RESUME,
    CMD_KILL,
})

VERBS_REQUIRING_ARGS = frozenset({CMD_BLOCK, CMD_INVESTIGATE, CMD_DISCUSS})


@dataclass(frozen=True)
class Command:
    """A successfully parsed operator command."""

    verb: str
    args: str  # whatever followed the verb (stripped); may be empty


@dataclass(frozen=True)
class CommandError:
    """An OPERATOR command that came from the operator but is malformed."""

    reason: str
    valid_verbs: tuple[str, ...] = tuple(sorted(VALID_VERBS))


def parse_operator_command(
    body: str,
    *,
    author_user_id: int,
    operator_user_id: int,
) -> Optional[Command] | CommandError:
    """Parse a PR comment as an operator command.

    Returns:
      - None: the comment is not addressed to the dispatcher (no OPERATOR
        prefix, or from a non-operator account). The dispatcher takes no
        operator action; it may still treat the comment as ordinary review
        input.
      - CommandError: the comment IS from the operator and has the OPERATOR
        prefix, but the verb is unrecognized or the args are wrong. The
        dispatcher should reply with a no-op message listing valid verbs.
      - Command: a valid operator command. The dispatcher executes it.
    """
    if not body:
        return None

    # First non-empty line. Do NOT strip before the prefix check, so a
    # body of "OPERATOR " (prefix only, no verb) is recognized as an
    # operator command with missing_verb rather than silently ignored.
    first_line = ""
    for line in body.splitlines() or [body]:
        if line.strip():
            first_line = line
            break
    if not first_line and body.strip():
        # No newlines in body
        first_line = body
    if not first_line:
        return None

    if not first_line.startswith(OPERATOR_PREFIX):
        return None

    # Author check happens AFTER prefix check, so that random users can't
    # cause the dispatcher to reply to their typo'd "OPERATOR APROVE" attempts.
    # Only the configured operator's malformed commands get a no-op reply.
    if author_user_id != operator_user_id:
        return None

    rest = first_line[len(OPERATOR_PREFIX):].strip()
    if not rest:
        return CommandError(reason="missing_verb")

    parts = rest.split(maxsplit=1)
    verb = parts[0]
    args = parts[1] if len(parts) > 1 else ""

    if verb not in VALID_VERBS:
        return CommandError(reason=f"unknown_verb:{verb}")

    if verb in VERBS_REQUIRING_ARGS and not args.strip():
        return CommandError(reason=f"verb_requires_argument:{verb}")

    return Command(verb=verb, args=args)


def format_command_error_reply(error: CommandError) -> str:
    """Build the no-op reply for a malformed OPERATOR command."""
    return (
        f"Operator command not recognized (reason: `{error.reason}`).\n\n"
        "The PR state is unchanged. Valid commands:\n\n"
        + "\n".join(f"- `OPERATOR {verb}`" for verb in error.valid_verbs)
        + "\n\nSee Section 7 of `docs/tradewatcher_dispatcher_design.md` for "
        "the full command surface and required arguments."
    )


__all__ = [
    "OPERATOR_PREFIX",
    "CMD_APPROVE",
    "CMD_BLOCK",
    "CMD_INVESTIGATE",
    "CMD_DISCUSS",
    "CMD_PAUSE",
    "CMD_RESUME",
    "CMD_KILL",
    "VALID_VERBS",
    "VERBS_REQUIRING_ARGS",
    "Command",
    "CommandError",
    "parse_operator_command",
    "format_command_error_reply",
]
