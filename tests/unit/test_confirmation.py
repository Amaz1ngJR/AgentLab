import json
import pytest
from app.agent.approval import InteractivePolicy, AutoApprove
from app.agent.runtime import AgentSession
from app.models.protocol import ModelResponse, ToolCall
from app.tools.registry import ToolRegistry
from app.tools.builtin.confirmation import REQUEST_CONFIRMATION
from tests.support.delegation import FakeRouter, response

ARGS = {"summary": "重启 Edge", "details": "关闭所有 Edge 窗口，再以调试模式启动。替代：手动登录。"}

@pytest.mark.parametrize("choice,approved,cancelled", [
    ("yes", True, False), ("no", False, False), ("modify", False, True),
    ("enable_auto_execute", True, False), (None, False, True),
])
def test_confirmation_menu_four_choices_and_full_impact(monkeypatch, choice, approved, cancelled):
    menus = []
    def select(**kwargs):
        menus.append(kwargs)
        return choice
    monkeypatch.setattr("app.util.menu.select_menu", select)
    policy = InteractivePolicy()
    policy._always.add("request_confirmation")  # Explicit questions never use a remembered grant.
    result = policy.request_tool(REQUEST_CONFIRMATION, "request_confirmation", ARGS)
    assert result.approved == approved and result.cancelled == cancelled
    assert [value for _, value in menus[0]["choices"]] == ["yes", "no", "modify", "enable_auto_execute"]
    assert ARGS["details"] in menus[0]["header_lines"]
    if choice == "enable_auto_execute":
        assert result.feedback == "__enable_auto_execute__"

@pytest.mark.parametrize("initial_auto,choice", [(False, "yes"), (True, "yes"), (False, "enable_auto_execute"), (True, "no"), (True, "modify")])
def test_runtime_confirmation_is_not_skipped_by_auto_mode(monkeypatch, initial_auto, choice):
    seen = []
    monkeypatch.setattr("app.util.menu.select_menu", lambda **kwargs: seen.append(kwargs) or choice)
    tools = ToolRegistry()
    tools.register(REQUEST_CONFIRMATION)
    call = ModelResponse("", [ToolCall("confirm", "request_confirmation", ARGS)], {}, [])
    model = FakeRouter([call, response("后续处理")])
    session = AgentSession(model, tools, approval=InteractivePolicy(), max_steps=4)
    if initial_auto:
        session.enable_auto_execute()
    session.chat("继续")
    assert len(seen) == 1
    if choice == "enable_auto_execute":
        assert session.auto_execute_enabled
    if choice == "modify":
        assert len(model.calls) == 1
    else:
        assert len(model.calls) == 2
    if choice == "no":
        assert "denied" in json.dumps(model.calls[-1]["messages"]).lower()


def test_headless_auto_approve_does_not_answer_explicit_question(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    result = AutoApprove().request_tool(REQUEST_CONFIRMATION, "request_confirmation", ARGS)
    assert not result.approved and result.cancelled


@pytest.mark.parametrize("auto,choice", [(True, "yes"), (False, "enable_auto_execute")])
def test_task_executor_uses_same_confirmation_menu(monkeypatch, auto, choice):
    from app.agent.executor import Executor
    from app.agent.tasks import Task
    seen, enabled = [], []
    monkeypatch.setattr("app.util.menu.select_menu", lambda **kwargs: seen.append(kwargs) or choice)
    tools = ToolRegistry()
    tools.register(REQUEST_CONFIRMATION)
    model = FakeRouter([
        ModelResponse("", [ToolCall("confirm", "request_confirmation", ARGS)], {}, []),
        response("done"),
    ])
    executor = Executor(model, tools, approval=InteractivePolicy())
    outcome = executor.run_task(Task(id="t", content="继续操作"), [], system="test", max_steps=3,
                                auto_execute=auto, on_auto_execute=lambda: enabled.append(True))
    assert len(seen) == 1
    assert outcome.text == "done"
    assert enabled == ([True] if choice == "enable_auto_execute" else [])
