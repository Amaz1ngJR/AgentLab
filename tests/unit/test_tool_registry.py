"""ToolDescriptor、分级审批和统一审计的离线测试。"""

import pytest

from app.tools.builtin import default_tools
from app.tools.registry import Tool, ToolDescriptor, ToolRegistry, extract_web_urls


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


def test_direct_conversation_does_not_expose_tools():
    registry = ToolRegistry()
    for tool in default_tools():
        registry.register(tool)

    assert registry.schemas_for_task("请介绍下你自己", mode="direct") == []


def test_direct_mcp_capability_query_exposes_only_connected_mcp_tools():
    registry = ToolRegistry()
    registry.register(_descriptor(name="read_file", origin="builtin"))
    registry.register(_descriptor(
        name="browser_snapshot",
        origin="mcp",
        description="[MCP server: playwright] inspect page",
    ))
    registry.register(_descriptor(name="extension_tool", origin="extension"))

    schemas = registry.schemas_for_task("看下你的 MCP 能力", mode="direct")

    assert [item["name"] for item in schemas] == ["browser_snapshot"]
    assert "playwright" in schemas[0]["description"]

    context = registry.capability_context_for_task("看下你的 MCP 能力")
    assert "browser_snapshot" in context
    assert "server: playwright" in context
    assert "不要声称未连接" in context


def test_mcp_capability_context_reports_no_connection_truthfully():
    registry = ToolRegistry()

    assert "未连接任何 MCP Server" in registry.capability_context_for_task(
        "有哪些 Model Context Protocol 工具"
    )
    assert registry.capability_context_for_task("请介绍下你自己") == ""


def test_direct_web_capability_query_exposes_web_and_browser_tools():
    registry = ToolRegistry()
    for tool in default_tools():
        registry.register(tool)
    registry.register(_descriptor(
        name="browser_snapshot",
        origin="mcp",
        target_type="browser",
        host="playwright",
    ))
    registry.register(_descriptor(name="unrelated_extension", origin="extension"))

    schemas = registry.schemas_for_task("你当前有能力看到网页内容吗", mode="direct")
    names = {item["name"] for item in schemas}

    assert names == {"web_search", "web_fetch", "browser_snapshot"}
    context = registry.capability_context_for_task("你当前有能力看到网页内容吗")
    assert "当前网页访问能力" in context
    assert "browser_snapshot" in context
    assert "不要假设用户已经提供了某个网页" in context


def test_direct_browser_action_uses_mcp_instead_of_filesystem_or_shell():
    registry = ToolRegistry()
    for tool in default_tools():
        registry.register(tool)
    for name in ("browser_navigate", "browser_snapshot", "browser_type", "browser_click"):
        registry.register(_descriptor(
            name=name,
            origin="mcp",
            target_type="browser",
            host="playwright",
        ))

    task = "请直接在本地打开网页，然后在网页里面写入答案"
    names = {item["name"] for item in registry.schemas_for_task(task, mode="direct")}

    assert {"browser_navigate", "browser_snapshot", "browser_type"} <= names
    assert "write_file" not in names
    assert "edit_file" not in names
    assert "shell" not in names
    context = registry.capability_context_for_task(task)
    assert "必须使用 browser_*" in context
    assert "不得使用 shell" in context
    planner_context = registry.planner_context_for_task(task)
    assert "browser_navigate" in planner_context
    assert "禁止规划 xdg-open" in planner_context


def test_specific_url_request_exposes_fetch_and_minimal_browser_tools():
    registry = ToolRegistry()
    registry.register(_descriptor(name="web_fetch", origin="builtin"))
    registry.register(_descriptor(name="web_search", origin="builtin"))
    registry.register(_descriptor(
        name="browser_navigate",
        origin="mcp",
        target_type="browser",
    ))
    registry.register(_descriptor(
        name="browser_snapshot",
        origin="mcp",
        target_type="browser",
    ))
    registry.register(_descriptor(
        name="browser_click",
        origin="mcp",
        target_type="browser",
    ))

    schemas = registry.schemas_for_task(
        "请查看 https://example.com 并回答问题", mode="direct"
    )
    assert {item["name"] for item in schemas} == {
        "web_fetch", "browser_navigate", "browser_snapshot",
    }
    context = registry.capability_context_for_task(
        "请查看 https://example.com 并回答问题"
    )
    assert "尚无网页工具结果" in context
    assert "不得重复调用同一 URL" in context
    assert "不得在未调用工具时声称无法查看网页" in context


def test_open_url_request_uses_browser_without_fetch_competition():
    registry = ToolRegistry()
    registry.register(_descriptor(name="web_fetch", origin="builtin"))
    for name in ("browser_navigate", "browser_snapshot", "browser_click"):
        registry.register(_descriptor(
            name=name,
            origin="mcp",
            target_type="browser",
        ))

    task = "请打开 https://leetcode.cn/problems/example 这个网页，并帮我写答案"
    schemas = registry.schemas_for_task(task, mode="direct")

    assert {item["name"] for item in schemas} == {
        "browser_navigate", "browser_snapshot", "browser_click",
    }
    context = registry.capability_context_for_task(task)
    assert "本轮不要调用 web_fetch" in context
    assert "先调用 browser_navigate" in context


def test_url_extraction_stops_before_adjacent_chinese_request_text():
    text = (
        "请看https://leetcode.cn/problems/example/description/?envType=daily-question"
        "这个力扣题，给出c++答案"
    )

    assert extract_web_urls(text) == (
        "https://leetcode.cn/problems/example/description/?envType=daily-question",
    )


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
