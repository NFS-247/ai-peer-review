"""Tests for scripts.dispatcher.verdict."""

import pytest

from scripts.dispatcher.verdict import (
    VERDICT_APPROVE,
    VERDICT_REQUEST_CHANGES,
    Verdict,
    build_verdict,
    compute_diff_sha256,
    parse_ai_json_response,
    parse_verdict_from_comment,
)


def _good_verdict() -> Verdict:
    return build_verdict(
        reviewer="claude",
        verdict=VERDICT_APPROVE,
        pr_number=42,
        head_sha="abc1234deadbeef",
        diff_sha256="a" * 64,
        tier="routine",
        round_=1,
    )


def test_build_verdict_roundtrip():
    v = _good_verdict()
    block = v.to_yaml_block()
    body = "Some reasoning text\n\n" + block + "\n"
    parsed = parse_verdict_from_comment(body)
    assert parsed is not None
    assert parsed.reviewer == "claude"
    assert parsed.verdict == VERDICT_APPROVE
    assert parsed.pr_number == 42
    assert parsed.tier == "routine"
    assert parsed.round == 1


def test_build_verdict_rejects_bad_reviewer():
    with pytest.raises(ValueError):
        build_verdict(
            reviewer="grok",
            verdict=VERDICT_APPROVE,
            pr_number=1,
            head_sha="abc1234",
            diff_sha256="a" * 64,
            tier="routine",
            round_=1,
        )


def test_build_verdict_rejects_bad_verdict_value():
    with pytest.raises(ValueError):
        build_verdict(
            reviewer="claude",
            verdict="lgtm",
            pr_number=1,
            head_sha="abc1234",
            diff_sha256="a" * 64,
            tier="routine",
            round_=1,
        )


def test_build_verdict_rejects_bad_sha_length():
    with pytest.raises(ValueError):
        build_verdict(
            reviewer="claude",
            verdict=VERDICT_APPROVE,
            pr_number=1,
            head_sha="abc1234",
            diff_sha256="too_short",
            tier="routine",
            round_=1,
        )


def test_parse_returns_none_for_freeform_lgtm():
    body = "LGTM! Approving this change."
    assert parse_verdict_from_comment(body) is None


def test_parse_returns_none_for_native_github_approve():
    # GitHub native approval text never contains the verdict fence.
    body = "Approved by reviewer Foo"
    assert parse_verdict_from_comment(body) is None


def test_parse_returns_last_block_when_multiple():
    block1 = (
        "```tradewatcher-verdict\n"
        "reviewer: claude\n"
        "verdict: request_changes\n"
        "pr_number: 7\n"
        "head_sha: abc1234deadbeef\n"
        "diff_sha256: " + "a" * 64 + "\n"
        "tier: routine\n"
        "round: 1\n"
        "emitted_at: 2026-06-09T12:00:00Z\n"
        "```\n"
    )
    block2 = (
        "```tradewatcher-verdict\n"
        "reviewer: claude\n"
        "verdict: approve\n"
        "pr_number: 7\n"
        "head_sha: abc1234deadbeef\n"
        "diff_sha256: " + "a" * 64 + "\n"
        "tier: routine\n"
        "round: 2\n"
        "emitted_at: 2026-06-09T12:30:00Z\n"
        "```\n"
    )
    parsed = parse_verdict_from_comment(block1 + "\n\nFollow-up:\n\n" + block2)
    assert parsed is not None
    assert parsed.verdict == VERDICT_APPROVE
    assert parsed.round == 2


def test_parse_returns_none_for_malformed_block():
    body = (
        "```tradewatcher-verdict\n"
        "this is not a key: value line\n"
        "neither is this\n"
        "```\n"
    )
    assert parse_verdict_from_comment(body) is None


def test_compute_diff_sha256_stable():
    a = compute_diff_sha256("hello world\n")
    b = compute_diff_sha256("hello world\n")
    assert a == b
    assert len(a) == 64


def test_compute_diff_sha256_changes_with_input():
    a = compute_diff_sha256("hello\n")
    b = compute_diff_sha256("world\n")
    assert a != b


def test_parse_ai_json_response_happy():
    raw = '{"verdict": "approve", "reasoning": "looks fine"}'
    parsed = parse_ai_json_response(raw)
    assert parsed["verdict"] == VERDICT_APPROVE
    assert parsed["reasoning"] == "looks fine"


def test_parse_ai_json_response_rejects_invalid_verdict():
    raw = '{"verdict": "lgtm", "reasoning": "x"}'
    with pytest.raises(ValueError):
        parse_ai_json_response(raw)


def test_parse_ai_json_response_rejects_non_json():
    with pytest.raises(ValueError):
        parse_ai_json_response("Looks good to me!")


def test_parse_ai_json_response_rejects_missing_verdict():
    with pytest.raises(ValueError):
        parse_ai_json_response('{"reasoning": "x"}')


def test_parse_ai_json_response_rejects_non_object():
    with pytest.raises(ValueError):
        parse_ai_json_response('["approve"]')


# ---- HMAC signature (GPT review #5 on PR #74) ------------------------------

def test_signature_roundtrip_verifies():
    from scripts.dispatcher.verdict import (
        compute_verdict_signature,
        verify_verdict_signature,
    )
    v = _good_verdict()
    sig = compute_verdict_signature(v, "secret-1")
    assert verify_verdict_signature(v, sig, "secret-1") is True


def test_signature_wrong_secret_fails():
    from scripts.dispatcher.verdict import (
        compute_verdict_signature,
        verify_verdict_signature,
    )
    v = _good_verdict()
    sig = compute_verdict_signature(v, "secret-1")
    assert verify_verdict_signature(v, sig, "secret-2") is False


def test_signature_missing_fails():
    from scripts.dispatcher.verdict import verify_verdict_signature
    v = _good_verdict()
    assert verify_verdict_signature(v, None, "secret-1") is False
    assert verify_verdict_signature(v, "", "secret-1") is False


def test_parse_signed_requires_valid_signature():
    from scripts.dispatcher.verdict import (
        compute_verdict_signature,
        parse_signed_verdict_from_comment,
    )
    v = _good_verdict()
    good_sig = compute_verdict_signature(v, "secret-1")

    signed_body = "reasoning\n\n" + v.to_yaml_block(signature=good_sig)
    unsigned_body = "reasoning\n\n" + v.to_yaml_block()
    wrong_sig_body = "reasoning\n\n" + v.to_yaml_block(signature="deadbeef")

    assert parse_signed_verdict_from_comment(signed_body, "secret-1") is not None
    assert parse_signed_verdict_from_comment(unsigned_body, "secret-1") is None
    assert parse_signed_verdict_from_comment(wrong_sig_body, "secret-1") is None
    # right block, wrong secret
    assert parse_signed_verdict_from_comment(signed_body, "secret-2") is None


def test_tampered_verdict_field_breaks_signature():
    # Sign verdict=approve, then swap the verdict line to request_changes.
    from scripts.dispatcher.verdict import (
        compute_verdict_signature,
        parse_signed_verdict_from_comment,
    )
    v = _good_verdict()
    sig = compute_verdict_signature(v, "secret-1")
    body = "reasoning\n\n" + v.to_yaml_block(signature=sig)
    tampered = body.replace("verdict: approve", "verdict: request_changes")
    assert parse_signed_verdict_from_comment(tampered, "secret-1") is None
