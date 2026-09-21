"""测试全自动执行模式。"""
import pytest
from unittest.mock import Mock
from app.agent.approval import ApprovalResult, InteractivePolicy
from app.agent.mode_router import ExecutionMode
from app.agent.runtime import AgentSession
from app.tools.registry import ToolRegistry


def test_auto_execute_toggle():
    """测试全自动执行模式的开关。"""
    # 使用 Mock 对象简化测试
    llm = Mock()
    llm.capabilities = []
    tools = ToolRegistry()
    session = AgentSession(llm, tools)
    
    # 初始状态：关闭
    assert not session.auto_execute_enabled
    
    # 打开
    session.enable_auto_execute()
    assert session.auto_execute_enabled
    
    # 关闭
    session.disable_auto_execute()
    assert not session.auto_execute_enabled
    
    # 切换
    enabled = session.toggle_auto_execute()
    assert enabled
    assert session.auto_execute_enabled
    
    enabled = session.toggle_auto_execute()
    assert not enabled
    assert not session.auto_execute_enabled


def test_approval_auto_execute_option():
    """测试审批对话框包含自动执行选项。"""
    policy = InteractivePolicy()
    
    # 模拟构造选项
    choices, _ = policy._build_approval_choices(
        tool_name="shell",
        action="shell",
        tool=None,
        can_persist=True,
        shell_analysis=None,
    )
    
    # 检查包含"自动执行"选项
    labels = [label for label, _ in choices]
    assert "自动执行（本轮起）" in labels


def test_approval_result_auto_execute_signal():
    """测试审批结果能正确携带自动执行信号。"""
    result = ApprovalResult(approved=True, feedback="__enable_auto_execute__")
    
    assert result.approved
    assert result.feedback == "__enable_auto_execute__"
    assert not result.cancelled


def test_legacy_path_honours_cancel_token():
    """legacy(Direct)路径必须响应 CancelToken，否则全自动模式下 Esc 停不下来。

    回归测试：以前 _chat_legacy 不接收 cancel，有审批弹窗时 Esc 被菜单吸收，
    开启自动执行后弹窗消失，Esc 就此失效。现在应在模型调用前的安全检查点退出。
    """
    from app.agent.cancel import CancelToken

    llm = Mock()
    llm.capabilities = []
    session = AgentSession(llm, ToolRegistry())
    session._configured_mode = ExecutionMode.DIRECT  # 强制走 legacy 路径

    token = CancelToken()
    token.cancel()  # 进入循环前就已取消

    result = session.chat("随便做点什么", cancel=token)

    assert result == "已中断。"
    # 取消后不应再调用模型
    assert not llm.create_message.called


def test_legacy_path_ignores_unset_cancel_token():
    """未取消的 token 不应影响正常执行。"""
    from app.agent.cancel import CancelToken

    llm = Mock()
    llm.capabilities = []
    session = AgentSession(llm, ToolRegistry())
    session._configured_mode = ExecutionMode.DIRECT

    token = CancelToken()  # 未取消
    try:
        session.chat("说一句话", cancel=token)
    except Exception:
        pass  # Mock llm 会失败，只要不是被取消提前退出即可
    # 说明确实走到了模型调用，没被误判为取消
    assert llm.create_message.called


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
