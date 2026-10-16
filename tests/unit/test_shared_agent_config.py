import json

import pytest

from app.agent.profiles import load_agent_profiles
from app.agent.runtime import AgentSession
from app.config.loader import use_workspace_root
from app.models.protocol import ModelResponse, ToolCall
from tests.unit.test_execution_plan import FakeRouter, registry, response


@pytest.mark.parametrize("orchestrate", [False, True])
@pytest.mark.parametrize("max_task_steps", [None, 48])
def test_child_inherits_parent_budget_without_old_fixed_caps(tmp_path, orchestrate, max_task_steps):
    class Child(FakeRouter):
        rounds = 0

        def create_message(self, messages, **kwargs):
            self.rounds += 1
            if self.rounds <= 40:
                return ModelResponse("", [ToolCall(str(self.rounds), "read_file", {
                    "path": str(self.rounds),
                })], {"input_tokens": 1, "output_tokens": 1}, [])
            return response("CHILD_CONFIG_DONE")

    class Parent(FakeRouter):
        spawned = False

        def fork(self):
            return Child([])

        def create_message(self, messages, **kwargs):
            if not kwargs.get("tools"):
                return response(json.dumps({"version": 1, "tasks": [{
                    "id": "research", "content": "总结", "executor": "subagent",
                    "subagent": {"id": "child", "instruction": "总结", "role": "research"},
                }]}))
            if not orchestrate and not self.spawned:
                self.spawned = True
                return ModelResponse("", [ToolCall("spawn", "spawn_agent", {
                    "instruction": "总结",
                })], {}, [])
            return response("parent synthesis")

    session = AgentSession(Parent([]), registry(), orchestrate=orchestrate,
                           max_steps=120, max_task_steps=max_task_steps,
                           max_tool_calls=104, timeout_seconds=600)
    with use_workspace_root(tmp_path):
        assert session.chat("多个 agent 总结") == "parent synthesis"
    record = session.child_records[0]
    assert record["status"] == "succeeded", record["error"]
    assert record["consumed"]["model_rounds"] == 41
    assert record["consumed"]["tool_calls"] == 40
    assert record["spec"]["max_steps"] == (48 if max_task_steps else 59)
    assert record["spec"]["timeout_seconds"] > 300
    assert record["spec"]["max_tool_calls"] > 30
    assert record["budget"]["model_rounds_left"] < 120 - 41
    assert record["budget"]["tool_calls_left"] <= 104 - 40


def test_shared_budget_profile_loading():
    # Use the shipped example rather than a separate subagent configuration format.
    from pathlib import Path
    profiles = load_agent_profiles(Path("config/agents.example.yaml"))
    assert profiles["coder"].max_tool_calls == 120
    assert profiles["coder"].timeout_seconds == 600


@pytest.mark.parametrize("field,value", [("max_tool_calls", 0), ("timeout_seconds", 0),
                                        ("timeout_seconds", float("inf"))])
def test_shared_budget_rejects_invalid_limits(field, value):
    from app.agent.profiles import AgentProfile
    with pytest.raises(ValueError):
        AgentProfile("test", "test", "fake", **{field: value})
