import base64

import pytest

from agent.mutations.rules import REGISTRY, apply_chain


def test_plain_is_identity():
    assert apply_chain("do X", ["plain"]) == "do X"


def test_registry_has_distinct_named_strategies():
    # Count distinct strategies, not rows (feedback deck: mode collapse).
    assert set(REGISTRY) >= {"plain", "roleplay_wrapper", "base64_body", "korean_switch"}


def test_base64_rule_round_trips():
    out = apply_chain("secret request", ["base64_body"])
    token = out.strip().splitlines()[-1]
    assert base64.b64decode(token).decode() == "secret request"


def test_korean_switch_adds_korean_and_keeps_request():
    out = apply_chain("do X", ["korean_switch"])
    assert "do X" in out and any("가" <= c <= "힣" for c in out)


def test_chain_applies_left_to_right():
    out = apply_chain("do X", ["roleplay_wrapper", "korean_switch"])
    assert "do X" in out


def test_unknown_rule_raises():
    with pytest.raises(KeyError):
        apply_chain("x", ["no_such_rule"])
