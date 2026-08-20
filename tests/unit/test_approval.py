"""离线测试：审批策略。不需要网络或模型。"""
from unittest.mock import patch

from app.agent.approval import (
    AutoApprove,
    DenyAll,
    InteractivePolicy,
    request_tool_approval,
)
from app.tools.registry import ToolDescriptor


def test_auto_approve():
    assert AutoApprove().request("write_file", {"path": "/tmp/x"}) is True


def test_deny_all():
    assert DenyAll().request("write_file", {"path": "/tmp/x"}) is False


def test_interactive_yes():
    """select_menu 返回 'yes' 时允许这次。"""
    with patch("app.util.menu.select_menu", return_value="yes"):
        assert InteractivePolicy().request("write_file", {}) is True


def test_interactive_no():
    """select_menu 返回 'no' 时拒绝。"""
    with patch("app.util.menu.select_menu", return_value="no"):
        assert InteractivePolicy().request("write_file", {}) is False


def test_interactive_always_caches_decision():
    """选 'always' 后,该工具名加入白名单,第二次不再调用 menu。"""
    policy = InteractivePolicy()
    with patch("app.util.menu.select_menu", return_value="always") as menu_mock:
        assert policy.request("write_file", {}) is True
        assert menu_mock.call_count == 1
    # 第二次:menu 不应再被调用,直接放行
    with patch("app.util.menu.select_menu") as menu_mock2:
        assert policy.request("write_file", {}) is True
        assert menu_mock2.call_count == 0


def test_interactive_cancel_treated_as_no():
    """用户按 Esc / Ctrl-C(menu 返回 None)视为拒绝。"""
    with patch("app.util.menu.select_menu", return_value=None):
        assert InteractivePolicy().request("write_file", {}) is False


def test_interactive_header_includes_tool_context():
    """传给 menu 的 header_lines 必须包含工具名与参数,方便用户判断。"""
    captured = {}

    def fake_menu(choices, header_lines, title, **_):
        captured["header_lines"] = header_lines
        captured["choices"] = choices
        return "no"

    with patch("app.util.menu.select_menu", side_effect=fake_menu):
        InteractivePolicy().request("write_file", {"path": "/tmp/x.txt"})

    assert any("write_file" in line for line in captured["header_lines"])
    assert any("/tmp/x.txt" in line for line in captured["header_lines"])
    # 四个选项,值分别是 yes / always / modify / no
    assert [c[1] for c in captured["choices"]] == ["yes", "always", "modify", "no"]


def test_outside_workspace_approval_cannot_be_remembered():
    captured = {}

    def fake_menu(choices, **_):
        captured["choices"] = choices
        return "yes"

    with patch("app.util.menu.select_menu", side_effect=fake_menu):
        assert InteractivePolicy().request(
            "read_file_outside_workspace",
            {"path": "/outside/file"},
        )

    assert [c[1] for c in captured["choices"]] == ["yes", "modify", "no"]


def test_clear_session_images_approval_cannot_be_remembered():
    captured = {}

    def fake_menu(choices, **_):
        captured["choices"] = choices
        return "yes"

    with patch("app.util.menu.select_menu", side_effect=fake_menu):
        assert InteractivePolicy().request(
            "clear_session_images", {"session_id": "s1"},
        )
    assert [choice[1] for choice in captured["choices"]] == ["yes", "modify", "no"]


    for action in ("shell", "terminal_open", "terminal_send"):
        captured = {}

        def fake_menu(choices, **_):
            captured["choices"] = choices
            return "yes"

        with patch("app.util.menu.select_menu", side_effect=fake_menu):
            assert InteractivePolicy().request(action, {"command": "pwd"})

        assert [c[1] for c in captured["choices"]] == ["yes", "modify", "no"]


def test_structured_approval_shows_risk_and_target():
    captured = {}
    tool = ToolDescriptor(
        name="browser_click",
        description="click",
        input_schema={"type": "object", "properties": {}},
        executor=lambda _: "ok",
        risk="browser_control",
        target_type="browser",
        scope="mcp_server:playwright",
        origin="mcp",
        host="playwright",
    )

    def fake_menu(choices, header_lines, **_):
        captured["choices"] = choices
        captured["header_lines"] = header_lines
        return "yes"

    with patch("app.util.menu.select_menu", side_effect=fake_menu):
        assert request_tool_approval(
            InteractivePolicy(),
            tool,
            "browser_click",
            {"ref": "x"},
        )

    assert [c[1] for c in captured["choices"]] == ["yes", "modify", "no"]
    assert "风险: browser_control" in captured["header_lines"]
    assert "目标: browser / mcp_server:playwright / playwright" in captured["header_lines"]
    assert "来源: mcp" in captured["header_lines"]


def test_request_tool_approval_keeps_legacy_policy_compatible():
    class LegacyPolicy:
        def __init__(self):
            self.calls = []

        def request(self, action, args):
            self.calls.append((action, args))
            return True

    policy = LegacyPolicy()
    tool = ToolDescriptor(
        name="write_file",
        description="write",
        input_schema={"type": "object", "properties": {}},
        executor=lambda _: "ok",
        risk="write",
    )

    assert request_tool_approval(policy, tool, "write_file", {"path": "x"})
    assert policy.calls == [("write_file", {"path": "x"})]
