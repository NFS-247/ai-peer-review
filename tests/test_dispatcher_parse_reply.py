"""Tests for scripts.dispatcher.parse_reply."""

from scripts.dispatcher.parse_reply import (
    CMD_APPROVE,
    CMD_BLOCK,
    CMD_KILL,
    CMD_PAUSE,
    Command,
    CommandError,
    format_command_error_reply,
    parse_operator_command,
)


OP_ID = 12345
OTHER_ID = 67890


def test_no_prefix_returns_none():
    result = parse_operator_command(
        "lgtm",
        author_user_id=OP_ID,
        operator_user_id=OP_ID,
    )
    assert result is None


def test_non_operator_with_prefix_returns_none():
    # Random user typing OPERATOR ... should not trigger any reply.
    result = parse_operator_command(
        "OPERATOR APPROVE",
        author_user_id=OTHER_ID,
        operator_user_id=OP_ID,
    )
    assert result is None


def test_operator_with_valid_approve_returns_command():
    result = parse_operator_command(
        "OPERATOR APPROVE",
        author_user_id=OP_ID,
        operator_user_id=OP_ID,
    )
    assert isinstance(result, Command)
    assert result.verb == CMD_APPROVE
    assert result.args == ""


def test_operator_with_block_and_reason():
    result = parse_operator_command(
        "OPERATOR BLOCK premarket data leak still possible",
        author_user_id=OP_ID,
        operator_user_id=OP_ID,
    )
    assert isinstance(result, Command)
    assert result.verb == CMD_BLOCK
    assert result.args == "premarket data leak still possible"


def test_operator_with_typo_returns_command_error():
    result = parse_operator_command(
        "OPERATOR APROVE",  # typo
        author_user_id=OP_ID,
        operator_user_id=OP_ID,
    )
    assert isinstance(result, CommandError)
    assert "unknown_verb" in result.reason


def test_operator_with_block_no_reason_returns_command_error():
    result = parse_operator_command(
        "OPERATOR BLOCK",
        author_user_id=OP_ID,
        operator_user_id=OP_ID,
    )
    assert isinstance(result, CommandError)
    assert "verb_requires_argument" in result.reason


def test_operator_pause_does_not_require_args():
    result = parse_operator_command(
        "OPERATOR PAUSE",
        author_user_id=OP_ID,
        operator_user_id=OP_ID,
    )
    assert isinstance(result, Command)
    assert result.verb == CMD_PAUSE


def test_kill_works():
    result = parse_operator_command(
        "OPERATOR KILL",
        author_user_id=OP_ID,
        operator_user_id=OP_ID,
    )
    assert isinstance(result, Command)
    assert result.verb == CMD_KILL


def test_only_first_line_matters_for_prefix():
    body = "Hey\nOPERATOR APPROVE"
    result = parse_operator_command(
        body,
        author_user_id=OP_ID,
        operator_user_id=OP_ID,
    )
    assert result is None  # Prefix must be on first non-empty line


def test_first_nonempty_line_is_prefix():
    body = "\n\n  \nOPERATOR APPROVE"
    result = parse_operator_command(
        body,
        author_user_id=OP_ID,
        operator_user_id=OP_ID,
    )
    assert isinstance(result, Command)
    assert result.verb == CMD_APPROVE


def test_empty_command_returns_command_error():
    result = parse_operator_command(
        "OPERATOR ",
        author_user_id=OP_ID,
        operator_user_id=OP_ID,
    )
    assert isinstance(result, CommandError)
    assert result.reason == "missing_verb"


def test_lowercase_prefix_not_accepted():
    # Section 7: prefix is uppercase OPERATOR with trailing space.
    result = parse_operator_command(
        "operator approve",
        author_user_id=OP_ID,
        operator_user_id=OP_ID,
    )
    assert result is None


def test_format_command_error_reply_lists_valid_verbs():
    err = CommandError(reason="unknown_verb:APROVE")
    reply = format_command_error_reply(err)
    assert "unknown_verb" in reply
    assert "OPERATOR APPROVE" in reply
    assert "OPERATOR BLOCK" in reply
    assert "OPERATOR KILL" in reply
