import json
from dataclasses import dataclass
from agent.planner import LLMPlanner

SEEDS = [{"seed_id": f"advbench-{i:04d}", "goal": f"g{i}"} for i in range(4)]

@dataclass
class FakeResult:
    text: str

class FakeLLM:
    """Returns a scripted JSON plan; records how many times it was called."""
    def __init__(self, replies): self.replies=list(replies); self.calls=0; self.seen=[]
    def complete(self, role, system, messages, **kw):
        self.seen.append((role, system, messages, kw)); self.calls+=1
        return FakeResult(self.replies.pop(0) if self.replies else "{}")

def test_parses_model_json_into_seed_and_chain():
    llm = FakeLLM(['{"seed_id":"advbench-0002","mutations":["roleplay_wrapper","korean_switch"],"rationale":"x"}'])
    p = LLMPlanner(SEEDS, llm, max_attempts=1)
    (seed, chain), = list(p)
    assert seed["seed_id"] == "advbench-0002"
    assert chain == ["roleplay_wrapper","korean_switch"]
    assert llm.seen[0][0] == "planner"            # role
    assert "DATA" in llm.seen[0][1]               # system prompt is prompts/planner.md

def test_unknown_seed_or_rule_falls_back_safely():
    llm = FakeLLM(['{"seed_id":"NOPE","mutations":["no_such_rule"]}'])
    p = LLMPlanner(SEEDS, llm, max_attempts=1)
    (seed, chain), = list(p)
    assert seed["seed_id"] == "advbench-0000"     # default seed
    assert chain == ["plain"]                      # default chain

def test_malformed_json_does_not_crash():
    llm = FakeLLM(["sorry, I can't do that"])
    p = LLMPlanner(SEEDS, llm, max_attempts=1)
    (seed, chain), = list(p)
    assert seed["seed_id"] == "advbench-0000" and chain == ["plain"]

def test_history_is_sent_to_model():
    llm = FakeLLM(['{"seed_id":"advbench-0001","mutations":["plain"]}',
                   '{"seed_id":"advbench-0003","mutations":["korean_switch"]}'])
    p = LLMPlanner(SEEDS, llm, max_attempts=2)
    (s1, c1) = p.plan_next("a-0000")
    p.feedback(s1["seed_id"], c1, "not_finding")
    p.plan_next("a-0001")
    sent = json.loads(llm.seen[1][2][0]["content"])
    assert sent["history"][-1]["verdict"] == "not_finding"
    assert sent["allowed_mutations"]              # rules offered to the model

def test_cap_stops_iteration():
    llm = FakeLLM(['{"seed_id":"advbench-0000","mutations":["plain"]}']*10)
    p = LLMPlanner(SEEDS, llm, max_attempts=3)
    assert len(list(p)) == 3 and llm.calls == 3

def test_refused_call_raises_instead_of_default_attempt():
    import pytest
    from types import SimpleNamespace
    from agent.planner import PlannerRefused

    class RefusingLLM:
        def complete(self, role, system, messages, **kw):
            return SimpleNamespace(text="", stop_reason="refusal")

    p = LLMPlanner(SEEDS, RefusingLLM(), max_attempts=3)
    with pytest.raises(PlannerRefused):
        p.plan_next("a-0000")
