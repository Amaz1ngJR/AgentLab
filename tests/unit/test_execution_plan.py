"""ExecutionPlan、子 Agent 权限和真实 Runtime 的离线测试。"""
from __future__ import annotations

import json
import json
from pathlib import Path

from app.agent.approval import AutoApprove
from app.agent.cancel import CancelToken
from app.agent.execution_plan import (
    ExecutionPolicy,
    PlannedTask,
    parse_execution_plan,
    validate_execution_plan,
)
from app.agent.subagent_runtime import RestrictedToolRegistry, SubagentRuntimeFactory
from app.agent.subagents import SubagentSpec
from app.agent.tasks import Task
from app.models.protocol import ModelResponse, ToolCall
from app.tools.registry import Tool, ToolRegistry


class FakeRouter:
    def __init__(self, responses: list[ModelResponse]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def create_message(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        if self.responses:
            return self.responses.pop(0)
        return ModelResponse("done", [], {"input_tokens": 1, "output_tokens": 1}, [])

    @staticmethod
    def format_tool_results(results):
        return [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": result.tool_call_id,
                "content": result.output,
                "is_error": result.is_error,
            } for result in results],
        }]


def response(text: str) -> ModelResponse:
    return ModelResponse(text, [], {"input_tokens": 1, "output_tokens": 1}, [])


def registry() -> ToolRegistry:
    tools = ToolRegistry()
    tools.register(Tool("read_file", "read", {}, lambda args: "read"))
    tools.register(Tool("edit_file", "edit", {}, lambda args: "edited", requires_approval=False))
    tools.register(Tool("shell", "shell", {}, lambda args: "ran", requires_approval=False))
    return tools


def test_parse_and_validate_subagent_plan():
    plan = parse_execution_plan({
        "version": 1,
        "execution": {"mode": "hybrid", "max_parallel": 2},
        "tasks": [{
            "id": "t1",
            "content": "调查代码",
            "executor": "subagent",
            "subagent": {
                "id": "research-1",
                "role": "research",
                "instruction": "读取代码并输出摘要",
                "read_only": True,
                "tools": ["read_file"],
            },
        }],
    }, "调查")

    assert plan.subagent_specs[0].subagent_id == "research-1"
    assert validate_execution_plan(plan) == []


def test_invalid_subagent_permissions_are_rejected():
    plan = parse_execution_plan({
        "version": 1,
        "tasks": [{
            "id": "t1", "content": "写文件", "executor": "subagent",
            "subagent": {
                "id": "research-1", "role": "research", "read_only": False,
                "tools": ["edit_file", "delegate_subagent"],
            },
        }],
    }, "写文件")

    errors = validate_execution_plan(plan)
    assert any("只读角色不能写入" in error for error in errors)
    assert any("禁止工具" in error for error in errors)


def test_invalid_dependency_and_cycle_are_rejected():
    plan = parse_execution_plan({
        "version": 1,
        "tasks": [
            {"id": "a", "content": "a", "dependencies": ["missing"]},
            {"id": "b", "content": "b", "dependencies": ["c"]},
            {"id": "c", "content": "c", "dependencies": ["b"]},
        ],
    }, "目标")
    errors = validate_execution_plan(plan)
    assert any("不存在的依赖" in error for error in errors)
    assert any("循环" in error for error in errors)


def test_old_plan_is_main_agent_only():
    plan = parse_execution_plan({"tasks": [{"id": "t1", "content": "完成"}]}, "目标")
    assert plan.version == 0
    assert plan.policy.mode == "single_agent"
    assert plan.subagent_specs == []
    assert validate_execution_plan(plan) == []


def test_restricted_registry_cannot_execute_unlisted_tool():
    source = registry()
    restricted = RestrictedToolRegistry(source, {"read_file"})
    assert [item["name"] for item in restricted.schemas()] == ["read_file"]
    output, is_error = restricted.execute("edit_file", {})
    assert is_error is True
    assert "not allowed" in output


def test_runtime_factory_uses_independent_child_runtime(tmp_path: Path):
    router = FakeRouter([response("child completed")])
    factory = SubagentRuntimeFactory(
        llm=router,
        tools=registry(),
        approval=AutoApprove(),
        parent_system="parent system",
        max_steps=2,
    )
    result = factory.delegate(
        SubagentSpec("child-1", "执行只读任务", role="research", read_only=True,
                     tools=("read_file",), max_steps=1),
        tmp_path,
        CancelToken(),
    )
    assert result.status == "succeeded"
    assert result.output
    assert "child-1" not in result.output
    assert router.calls
def test_factory_propagates_child_failure():
    router = FakeRouter([
        response(json.dumps({"tasks": [{"id": "t1", "content": "执行任务"}]})),
        ModelResponse("", [ToolCall("c1", "read_file", {})], {"input_tokens": 1, "output_tokens": 1}, []),
    ])
    factory = SubagentRuntimeFactory(
        llm=router, tools=registry(), approval=AutoApprove(), max_steps=1,
    )
    result = factory.delegate(
        SubagentSpec("child-fail", "执行任务", role="executor", tools=("read_file",), max_steps=1),
        Path("."), CancelToken(),
    )
    assert result.status == "failed"
    assert result.error


def test_factory_does_not_grant_write_to_read_only_role():
    restricted = RestrictedToolRegistry(registry(), {"read_file", "edit_file"})
    assert restricted.get("edit_file") is not None
    factory = SubagentRuntimeFactory(llm=FakeRouter([]), tools=registry())
    # 工厂再次按角色求交集，不能被 spec.tools 放大权限。
    spec = SubagentSpec("research", "读取", role="research", read_only=True,
                        tools=("read_file", "edit_file"))
    allowed = set(spec.tools) & set(__import__("app.agent.execution_plan", fromlist=["ROLE_TOOLS"]).ROLE_TOOLS[spec.role])
    assert "edit_file" not in allowed


def test_factory_propagates_cancel():
    token = CancelToken()
    token.cancel()
    factory = SubagentRuntimeFactory(llm=FakeRouter([]), tools=registry())
    import pytest
    from app.agent.cancel import Cancelled
    with pytest.raises(Cancelled):
        factory.delegate(SubagentSpec("cancel", "任务", role="research", read_only=True), Path("."), token)
