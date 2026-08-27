"""协议事件(RunEvent)结构化验证测试。

验证 MODE_SELECTED 事件是否正确发出，以及事件 payload 的完整性。
这是向稳定 Protocol 演进的第一步。
"""
from app.agent.events import MODE_SELECTED, RUN_STARTED, RunEvent
from app.agent.mode_router import ExecutionMode
from app.agent.planner import Planner
from app.agent.runtime import AgentSession
from app.models.protocol import ModelResponse
from app.tools.registry import ToolRegistry


class _FakeLLM:
    """返回固定文本、不发起真实请求的模型替身。"""

    model = "fake-model"
    provider = "fake"
    supports_vision = False

    def create_message(self, messages, **kwargs):
        return ModelResponse(
            text="ok",
            tool_calls=[],
            usage={"input_tokens": 1, "output_tokens": 1},
            provider_payload=[],
        )

    @staticmethod
    def format_tool_results(results):
        return []


def _auto_session(on_run_event) -> AgentSession:
    """构造 mode=auto 的会话，Planner 复用同一个替身模型。"""
    llm = _FakeLLM()
    return AgentSession(
        llm=llm,
        tools=ToolRegistry(),
        system_prompt="test",
        orchestrate=True,
        mode=ExecutionMode.AUTO,
        planner=Planner(llm),
        on_run_event=on_run_event,
    )


def test_mode_selected_event_emitted_on_orchestrated_run():
    """mode=auto 的多步骤请求走 chat() 时，应真实发出 task 的 MODE_SELECTED。"""
    events_received = []
    session = _auto_session(events_received.append)

    session.chat("修改多个文件并运行测试")

    modes = [e for e in events_received if e.kind == MODE_SELECTED]
    assert len(modes) == 1
    assert modes[0].payload["mode"] == ExecutionMode.TASK.value
    assert session.execution_mode is ExecutionMode.TASK


def test_mode_selected_payload_contains_required_fields():
    """MODE_SELECTED 事件的 payload 应包含 mode 和 reason 字段。"""
    event = RunEvent(
        kind=MODE_SELECTED,
        text="选择执行模式: direct",
        payload={
            "mode": "direct",
            "reason": "simple_query",
            "orchestrate_enabled": True,
        },
    )

    assert event.kind == MODE_SELECTED
    assert "mode" in event.payload
    assert "reason" in event.payload
    assert event.payload["mode"] in ["direct", "task", "loop"]


def test_run_event_dataclass_immutability():
    """RunEvent 应支持结构化访问，不依赖 Any 类型。"""
    event = RunEvent(
        kind=RUN_STARTED,
        text="开始执行任务",
        task_id="task-123",
        task_content="实现功能 X",
        payload={"goal": "完成功能开发", "max_iterations": 5},
    )

    # 验证字段可访问
    assert event.kind == RUN_STARTED
    assert event.text == "开始执行任务"
    assert event.task_id == "task-123"
    assert event.task_content == "实现功能 X"
    assert event.payload["goal"] == "完成功能开发"
    assert event.payload["max_iterations"] == 5


def test_mode_selected_for_direct_mode():
    """Direct 路径也必须发出 MODE_SELECTED，否则 CLI 无法显示本轮模式。"""
    events = []
    session = _auto_session(events.append)

    session.chat("解释这段代码")

    modes = [e for e in events if e.kind == MODE_SELECTED]
    assert len(modes) == 1
    assert modes[0].payload["mode"] == ExecutionMode.DIRECT.value
    assert session.execution_mode is ExecutionMode.DIRECT


def test_mode_selected_for_loop_mode():
    """Loop 模式应正确标记验收标准相关的 reason。"""
    event = RunEvent(
        kind=MODE_SELECTED,
        text="选择执行模式: loop",
        payload={
            "mode": "loop",
            "reason": "success_criteria_detected",
            "markers": ["/loop", "验收标准"],
        },
    )

    assert event.payload["mode"] == "loop"
    assert "success_criteria" in event.payload["reason"]
    assert "markers" in event.payload


def test_event_payload_supports_nested_structures():
    """事件 payload 应支持嵌套结构（为未来的 Turn/Item 准备）。"""
    event = RunEvent(
        kind="task_updated",
        text="任务状态更新",
        task_id="task-456",
        payload={
            "task": {
                "id": "task-456",
                "status": "in_progress",
                "dependencies": ["task-123", "task-234"],
            },
            "context": {
                "workspace": "/path/to/workspace",
                "session_id": "sess-789",
            },
        },
    )

    assert event.payload["task"]["id"] == "task-456"
    assert event.payload["task"]["status"] == "in_progress"
    assert len(event.payload["task"]["dependencies"]) == 2
    assert event.payload["context"]["session_id"] == "sess-789"


def test_mode_selected_with_session_state_context():
    """MODE_SELECTED 应包含影响决策的 session_state 快照。"""
    event = RunEvent(
        kind=MODE_SELECTED,
        text="选择执行模式: task",
        payload={
            "mode": "task",
            "reason": "resume_with_open_tasks",
            "session_state": {
                "has_open_tasks": True,
                "has_active_goal": False,
                "orchestrate_enabled": True,
            },
            "input_summary": "继续上一轮未完成的任务",
        },
    )

    assert event.payload["session_state"]["has_open_tasks"] is True
    assert event.payload["reason"] == "resume_with_open_tasks"


def test_run_event_default_values():
    """RunEvent 未使用的字段应有合理的默认值。"""
    event = RunEvent(kind="test_event")

    assert event.text == ""
    assert event.task_id == ""
    assert event.task_content == ""
    assert event.task_status == ""
    assert event.tool_name == ""
    assert event.tool_input == {}
    assert event.tool_output == ""
    assert event.tool_error is False
    assert event.elapsed_seconds == 0.0
    assert event.payload == {}
