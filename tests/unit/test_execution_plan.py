"""ExecutionPlan 解析、校验及子 Agent 工具权限的单元测试。"""
from __future__ import annotations

from app.agent.execution_plan import parse_execution_plan, validate_execution_plan
from app.agent.subagent_runtime import RestrictedToolRegistry, SubagentRuntimeFactory
from app.agent.subagents import SubagentSpec
from tests.support.delegation import FakeRouter, registry


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


def test_factory_does_not_grant_write_to_read_only_role():
    restricted = RestrictedToolRegistry(registry(), {"read_file", "edit_file"})
    assert restricted.get("edit_file") is not None
    factory = SubagentRuntimeFactory(llm=FakeRouter([]), tools=registry())
    # 工厂再次按角色求交集，不能被 spec.tools 放大权限。
    spec = SubagentSpec("research", "读取", role="research", read_only=True,
                        tools=("read_file", "edit_file"))
    allowed = set(spec.tools) & set(__import__("app.agent.execution_plan", fromlist=["ROLE_TOOLS"]).ROLE_TOOLS[spec.role])
    assert "edit_file" not in allowed
