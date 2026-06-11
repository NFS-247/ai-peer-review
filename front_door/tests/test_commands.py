"""Tests for operator-command bodies and signed approve URLs."""

import pytest

from front_door.app import commands as cmd


def test_approve_body():
    assert cmd.command_body("approve") == "OPERATOR APPROVE"


def test_block_and_investigate_need_text():
    assert cmd.command_body("block", "broker path unsafe") == "OPERATOR BLOCK broker path unsafe"
    assert cmd.command_body("investigate", "look at lookahead") == "OPERATOR INVESTIGATE look at lookahead"
    with pytest.raises(ValueError):
        cmd.command_body("block", "")
    with pytest.raises(ValueError):
        cmd.command_body("investigate", "   ")


def test_unknown_action_raises():
    with pytest.raises(ValueError):
        cmd.command_body("nuke", "x")


def test_text_flattened_to_first_line():
    # Newlines must not break the OPERATOR command off its first line.
    assert cmd.command_body("block", "line1\nline2") == "OPERATOR BLOCK line1 line2"


def test_sign_action_binds_to_pr_and_action():
    s = "secret"
    a = cmd.sign_action(s, repo="R", pr_number=114, action="approve")
    assert a == cmd.sign_action(s, repo="R", pr_number=114, action="approve")  # deterministic
    assert a != cmd.sign_action(s, repo="R", pr_number=115, action="approve")  # pr-bound
    assert a != cmd.sign_action(s, repo="R", pr_number=114, action="approve_merge")  # action-bound


def test_approve_url():
    # No base URL -> empty, regardless of secret.
    assert cmd.approve_url("", repo="R", pr_number=1, signing_secret="") == ""
    u = cmd.approve_url("https://x/exec", repo="R", pr_number=1, signing_secret="k")
    assert u.startswith("https://x/exec?")
    assert "repo=R" in u and "pr=1" in u and "action=approve" in u and "sig=" in u


def test_approve_url_refuses_unsigned():
    # A base URL with no secret is a silent-downgrade footgun -> must raise.
    try:
        cmd.approve_url("https://x/exec", repo="R", pr_number=1, signing_secret="")
        assert False, "expected ValueError"
    except ValueError:
        pass
