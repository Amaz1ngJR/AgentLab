"""离线测试:MCP 工具适配器 —— 用 fake manager,不连真 server。"""
from app.mcp.adapter import build_mcp_tools
from app.mcp.config import MCPServerConfig
from app.mcp.manager import MCPToolInfo


class FakeManager:
    """假 manager:tools() 返回预设列表,call_tool 记录调用并回固定值。"""

    def __init__(self, tools):
        self._tools = tools
        self.calls = []
        self.next_result = ("ok", False)

    def tools(self):
        return self._tools

    def call_tool(self, server, name, args, timeout=60.0):
        self.calls.append((server, name, args, timeout))
        return self.next_result


def _info(name, server="playwright"):
    return MCPToolInfo(server=server, name=name, description=f"{name} desc",
                       input_schema={"type": "object", "properties": {}})


def test_maps_tools_to_registry_tools():
    mgr = FakeManager([_info("browser_navigate"), _info("browser_click")])
    tools = build_mcp_tools(mgr, [MCPServerConfig(name="playwright")])
    names = {t.name for t in tools}
    assert names == {"browser_navigate", "browser_click"}


def test_skips_name_collision_with_builtin():
    mgr = FakeManager([_info("read_file"), _info("browser_navigate")])
    tools = build_mcp_tools(mgr, [MCPServerConfig(name="playwright")],
                            reserved_names={"read_file"})
    names = {t.name for t in tools}
    assert "read_file" not in names          # 内置不被覆盖
    assert "browser_navigate" in names


def test_auto_approve_whitelist():
    mgr = FakeManager([_info("browser_snapshot"), _info("browser_click")])
    cfg = MCPServerConfig(name="playwright", auto_approve=["browser_snapshot"])
    tools = {t.name: t for t in build_mcp_tools(mgr, [cfg])}
    assert tools["browser_snapshot"].requires_approval is False  # 白名单免审批
    assert tools["browser_click"].requires_approval is True      # 其余需审批


def test_executor_forwards_to_manager():
    mgr = FakeManager([_info("browser_navigate")])
    tool = build_mcp_tools(mgr, [MCPServerConfig(name="playwright")])[0]
    mgr.next_result = ("navigated to example.com", False)
    out = tool.executor({"url": "http://example.com"})
    assert out == "navigated to example.com"
    assert mgr.calls[0][:3] == ("playwright", "browser_navigate", {"url": "http://example.com"})


def test_executor_marks_mcp_error():
    mgr = FakeManager([_info("browser_click")])
    tool = build_mcp_tools(mgr, [MCPServerConfig(name="playwright")])[0]
    mgr.next_result = ("element not found", True)
    out = tool.executor({"ref": "x"})
    assert out.startswith("[mcp error]")
    assert "element not found" in out


def test_duplicate_tool_name_across_servers_skipped():
    mgr = FakeManager([_info("dup", server="a"), _info("dup", server="b")])
    tools = build_mcp_tools(mgr, [MCPServerConfig(name="a"), MCPServerConfig(name="b")])
    assert len([t for t in tools if t.name == "dup"]) == 1
