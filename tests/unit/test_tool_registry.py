"""ToolDescriptor、分级审批和统一审计的离线测试。"""

import pytest

from app.tools.builtin import default_tools
from app.tools.registry import Tool, ToolDescriptor, ToolRegistry


def _descriptor(**overrides):
    values = {
        "name": "sample",
        "description": "sample tool",
        "input_schema": {"type": "object", "properties": {}},
        "executor": lambda args: f"ok:{args.get('value', '')}",
    }
    values.update(overrides)
    return ToolDescriptor(**values)


def test_tool_is_backward_compatible_alias():
    assert Tool is ToolDescriptor
    assert isinstance(_descriptor(), Tool)


def test_legacy_positional_approval_arguments_remain_compatible():
    tool = Tool(
        "legacy",
        "legacy tool",
        {"type": "object", "properties": {}},
        lambda _: "ok",
        True,
        lambda args: "legacy_external" if args.get("external") else None,
    )
    assert tool.approval_action({}) == "legacy"
    assert tool.approval_action({"external": True}) == "legacy_external"


def test_risk_drives_default_approval():
    assert _descriptor(risk="read").approval_action({}) is None
    assert _descriptor(risk="network").approval_action({}) == "sample"
    assert _descriptor(risk="execute").approval_action({}) == "sample"


def test_explicit_approval_override_wins_over_risk_default():
    tool = _descriptor(risk="network", requires_approval=False)
    assert tool.approval_action({}) is None


def test_unknown_risk_is_rejected():
    with pytest.raises(ValueError, match="unknown tool risk"):
        _descriptor(risk="superuser")


def test_builtin_tools_declare_risk_and_target_metadata():
    tools = {tool.name: tool for tool in default_tools()}
    assert tools["read_file"].risk == "read"
    assert tools["write_file"].risk == "write"
    assert tools["code_search"].target_type == "filesystem"
    assert tools["shell"].risk == "execute"
    assert tools["web_search"].risk == "network"
    assert tools["web_fetch"].scope == "public_web"
    assert all(tool.origin == "builtin" for tool in tools.values())


def test_file_audit_summaries_do_not_store_file_contents():
    tools = {tool.name: tool for tool in default_tools()}
    write_args, _ = tools["write_file"].audit_summary(
        {"path": "note.txt", "content": "private file body"},
        "wrote note.txt",
    )
    _, read_result = tools["read_file"].audit_summary(
        {"path": "note.txt"},
        "private file body",
    )

    assert "private file body" not in write_args
    assert '"content_chars": 17' in write_args
    assert read_result == "returned_chars=17"


def test_registry_emits_structured_success_audit():
    events = []
    tool = _descriptor(
        risk="write",
        target_type="filesystem",
        scope="workspace",
        origin="builtin",
        host="local",
        requires_observation=True,
    )
    registry = ToolRegistry(audit_sink=events.append)
    registry.register(tool)

    output, is_error = registry.execute(
        "sample",
        {"value": "done"},
        approved_action="sample",
    )

    assert (output, is_error) == ("ok:done", False)
    assert len(events) == 1
    event = events[0]
    assert event.tool_name == "sample"
    assert event.risk == "write"
    assert event.target_type == "filesystem"
    assert event.scope == "workspace"
    assert event.origin == "builtin"
    assert event.host == "local"
    assert event.requires_observation is True
    assert event.approval_action == "sample"
    assert event.outcome == "completed"
    assert '"value": "done"' in event.args_summary


def test_registry_audits_missing_approval_and_user_denial():
    events = []
    registry = ToolRegistry(audit_sink=events.append)
    registry.register(_descriptor(risk="execute"))

    output, is_error = registry.execute("sample", {})
    registry.record_denied("sample", {"value": "no"}, approval_action="sample")

    assert is_error is True
    assert output == "approval required: sample"
    assert [event.outcome for event in events] == ["approval_required", "denied"]


def test_registry_audits_executor_error():
    events = []

    def explode(_):
        raise RuntimeError("boom")

    registry = ToolRegistry(audit_sink=events.append)
    registry.register(_descriptor(executor=explode))

    output, is_error = registry.execute("sample", {})

    assert is_error is True
    assert output == "RuntimeError: boom"
    assert events[0].outcome == "error"
    assert events[0].is_error is True




def test_registry_filters_visible_schemas_without_restricting_execution():
    registry = ToolRegistry()
    registry.register(_descriptor(name="inspect", capabilities={"inspect"}))
    registry.register(_descriptor(name="verify", capabilities={"verify"}))

    assert [item["name"] for item in registry.schemas(capabilities={"inspect"})] == ["inspect"]
    assert [item["name"] for item in registry.schemas(exclude={"verify"})] == ["inspect"]
    output, is_error = registry.execute("verify", {})
    assert (output, is_error) == ("ok:", False)


def test_registry_assigns_default_capabilities_to_builtin_tools():
    tools = {tool.name: tool for tool in default_tools()}
    assert "inspect" in tools["read_file"].capabilities
    assert "filesystem_write" in tools["edit_file"].capabilities
    assert "verify" in tools["shell"].capabilities
    assert "network" in tools["web_search"].capabilities


def test_schemas_for_task_keeps_simple_requests_small_and_adds_needed_tools():
    registry = ToolRegistry()
    for tool in default_tools():
        registry.register(tool)

    simple = {item["name"] for item in registry.schemas_for_task("解释这个函数")}
    coding = {item["name"] for item in registry.schemas_for_task("修改代码并运行测试")}
    web = {item["name"] for item in registry.schemas_for_task("查找最新技术文档")}

    assert simple == {"read_file", "code_search", "list_dir"}
    assert {"write_file", "edit_file", "shell"} <= coding
    assert {"web_search", "web_fetch"} <= web
    assert "shell" not in simple
    assert "web_search" not in coding


def test_schemas_for_stage_can_explicitly_include_a_tool():
    registry = ToolRegistry()
    registry.register(_descriptor(name="custom", capabilities={"custom"}))
    assert [item["name"] for item in registry.schemas_for_stage(
        "inspect", include={"custom"}
    )] == ["custom"]
    events = []

    def summarize(args, result):
        return f"value_length={len(args.get('value', ''))}", f"result_length={len(result)}"

    registry = ToolRegistry(audit_sink=events.append)
    registry.register(_descriptor(audit_redactor=summarize))
    registry.execute("sample", {"value": "private text"})

    assert events[0].args_summary == "value_length=12"
    assert events[0].result_summary == "result_length=15"
