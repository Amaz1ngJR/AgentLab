"""离线测试：AgentSession 多轮工具循环、审批、超限、错误。"""
from __future__ import annotations

from app.agent.approval import AutoApprove, DenyAll
from app.agent.runtime import (
    DEFAULT_SYSTEM_PROMPT,
    DENIED_MESSAGE,
    AgentSession,
    build_system_prompt,
)
from app.models.protocol import ModelResponse, ToolCall, ToolResult
from app.tools.registry import Tool, ToolRegistry


def test_build_system_prompt_injects_workspace():
    """传入 workspace 时,prompt 应包含该路径并声明"当前项目"指它。"""
    p = build_system_prompt("/Users/me/proj")
    assert "/Users/me/proj" in p
    assert "当前工作目录" in p
    # 仍保留默认准则(主动调查、不反问)
    assert "主动调查" in p


def test_build_system_prompt_without_workspace_is_default():
    """workspace 为空/None 时退回纯默认 prompt(向后兼容)。"""
    assert build_system_prompt(None) == DEFAULT_SYSTEM_PROMPT
    assert build_system_prompt("") == DEFAULT_SYSTEM_PROMPT


class FakeRouter:
    """按预设序列返回 ModelResponse,记录每次收到的 messages。"""

    def __init__(self, responses: list[ModelResponse]):
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def provider(self) -> str:
        return "fake"

    def create_message(
        self,
        messages,
        tools=None,
        system=None,
        temperature=None,
        max_tokens=4096,
        on_progress=None,
        on_text_delta=None,
        on_thinking_delta=None,
    ) -> ModelResponse:
        # 拷贝 messages 快照,避免被后续修改污染
        self.calls.append([dict(m) for m in messages])
        if not self._responses:
            return ModelResponse(text="(no more responses)", tool_calls=[],
                                 usage={"input_tokens": 0, "output_tokens": 0},
                                 provider_payload=[])
        return self._responses.pop(0)

    @staticmethod
    def format_tool_results(results: list[ToolResult]) -> list[dict]:
        """测试用的格式:Anthropic 风格,把多个 tool_result 包在一条 user 消息里。

        生产代码三家 adapter 各自实现自己的格式,Runtime 只关心拿到 list[dict]
        然后 messages.extend 即可。
        """
        blocks = [
            {
                "type": "tool_result",
                "tool_use_id": r.tool_call_id,
                "content": r.output,
                "is_error": r.is_error,
            }
            for r in results
        ]
        return [{"role": "user", "content": blocks}]


def _resp_text(text: str, in_tokens: int = 5, out_tokens: int = 3) -> ModelResponse:
    return ModelResponse(
        text=text,
        tool_calls=[],
        usage={"input_tokens": in_tokens, "output_tokens": out_tokens},
        provider_payload=[{"type": "text", "text": text}],
    )


def _resp_tool(tool_id: str, name: str, args: dict) -> ModelResponse:
    return ModelResponse(
        text="",
        tool_calls=[ToolCall(id=tool_id, name=name, arguments=args)],
        usage={"input_tokens": 5, "output_tokens": 1},
        provider_payload=[{"type": "tool_use", "id": tool_id, "name": name, "input": args}],
    )


def _registry_with(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _echo_tool() -> Tool:
    return Tool(
        name="echo",
        description="echo back",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
        executor=lambda args: f"echo: {args.get('msg', '')}",
    )


def _danger_tool() -> Tool:
    return Tool(
        name="danger",
        description="needs approval",
        input_schema={"type": "object", "properties": {}, "required": []},
        executor=lambda args: "executed danger",
        requires_approval=True,
    )


class RecordingApproval:
    def __init__(self, allowed: bool = True):
        self.allowed = allowed
        self.calls = []

    def request(self, tool_name, tool_input):
        self.calls.append((tool_name, tool_input))
        return self.allowed


def _dynamic_tool() -> Tool:
    return Tool(
        name="dynamic",
        description="conditionally needs approval",
        input_schema={"type": "object", "properties": {"outside": {"type": "boolean"}}},
        executor=lambda args: "dynamic executed",
        approval_resolver=lambda args: (
            "dynamic_outside_workspace" if args.get("outside") else None
        ),
    )


def _explode_tool() -> Tool:
    def boom(args):
        raise RuntimeError("intentional failure")
    return Tool(
        name="explode",
        description="raises",
        input_schema={"type": "object", "properties": {}, "required": []},
        executor=boom,
    )


# ── 用例 ──────────────────────────────────────────────────────────────────────


def test_chat_returns_text_when_no_tool_calls():
    router = FakeRouter([_resp_text("hello")])
    session = AgentSession(llm=router, tools=_registry_with(_echo_tool()))
    assert session.chat("hi") == "hello"
    assert session.last_turn_usage["output_tokens"] == 3
    assert session.cumulative_usage["output_tokens"] == 3


def test_single_tool_loop():
    router = FakeRouter([
        _resp_tool("call_1", "echo", {"msg": "world"}),
        _resp_text("done"),
    ])
    session = AgentSession(llm=router, tools=_registry_with(_echo_tool()))
    answer = session.chat("please echo world")
    assert answer == "done"
    # 第二轮请求应该带上工具结果
    second_call = router.calls[1]
    last_msg = second_call[-1]
    assert last_msg["role"] == "user"
    assert last_msg["content"][0]["type"] == "tool_result"
    assert last_msg["content"][0]["content"] == "echo: world"
    assert last_msg["content"][0]["is_error"] is False


def test_approval_denied_skips_execution():
    router = FakeRouter([
        _resp_tool("call_x", "danger", {}),
        _resp_text("ok, stopped"),
    ])
    session = AgentSession(
        llm=router,
        tools=_registry_with(_danger_tool()),
        approval=DenyAll(),
    )
    answer = session.chat("do dangerous thing")
    assert answer == "ok, stopped"
    second_call = router.calls[1]
    last_msg = second_call[-1]
    # 拒绝消息被作为 tool_result 喂回模型
    assert last_msg["content"][0]["content"] == DENIED_MESSAGE


def test_approval_denial_is_written_to_registry_audit():
    router = FakeRouter([
        _resp_tool("call_x", "danger", {}),
        _resp_text("stopped"),
    ])
    audit_events = []
    registry = ToolRegistry(audit_sink=audit_events.append)
    registry.register(_danger_tool())
    session = AgentSession(llm=router, tools=registry, approval=DenyAll())

    assert session.chat("do dangerous thing") == "stopped"
    assert len(audit_events) == 1
    assert audit_events[0].tool_name == "danger"
    assert audit_events[0].outcome == "denied"
    assert audit_events[0].approval_action == "danger"


def test_dynamic_approval_action_is_requested_and_granted():
    router = FakeRouter([
        _resp_tool("call_dynamic", "dynamic", {"outside": True}),
        _resp_text("done"),
    ])
    approval = RecordingApproval()
    session = AgentSession(
        llm=router,
        tools=_registry_with(_dynamic_tool()),
        approval=approval,
    )

    assert session.chat("use outside path") == "done"
    assert approval.calls == [
        ("dynamic_outside_workspace", {"outside": True}),
    ]
    tool_result = router.calls[1][-1]["content"][0]
    assert tool_result["content"] == "dynamic executed"
    assert tool_result["is_error"] is False


def test_tool_execution_error_is_reported():
    router = FakeRouter([
        _resp_tool("call_e", "explode", {}),
        _resp_text("noted"),
    ])
    session = AgentSession(
        llm=router,
        tools=_registry_with(_explode_tool()),
        approval=AutoApprove(),
    )
    session.chat("trigger explode")
    second_call = router.calls[1]
    tool_result = second_call[-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "RuntimeError" in tool_result["content"]


def test_max_steps_reached():
    # 模型一直请求工具,永远不给文本
    forever_tool_calls = [
        _resp_tool(f"c{i}", "echo", {"msg": str(i)}) for i in range(20)
    ]
    router = FakeRouter(forever_tool_calls)
    session = AgentSession(
        llm=router,
        tools=_registry_with(_echo_tool()),
        max_steps=3,
    )
    answer = session.chat("loop forever")
    assert "最大步数" in answer
    # 只调了 max_steps 次模型
    assert len(router.calls) == 3


def test_progress_and_text_callbacks_invoked():
    seen_progress: list[dict] = []
    seen_text: list[str] = []

    class TextStreamingFake(FakeRouter):
        def create_message(self, messages, tools=None, system=None,
                           temperature=None, max_tokens=4096,
                           on_progress=None, on_text_delta=None,
                           on_thinking_delta=None):
            if on_text_delta:
                on_text_delta("hel")
                on_text_delta("lo")
            if on_progress:
                on_progress({"input_tokens": 7, "output_tokens": 2})
            return super().create_message(messages, tools=tools, system=system)

    router = TextStreamingFake([_resp_text("hello")])
    session = AgentSession(
        llm=router,
        tools=_registry_with(_echo_tool()),
        on_event=lambda ev: seen_text.append(ev.text) if ev.kind == "text" else None,
        progress=lambda label: _ProgressHandle(seen_progress, seen_text),
    )
    session.chat("hi")
    assert any(p.get("output_tokens") == 2 for p in seen_progress)


class _ProgressHandle:
    """测试用的 progress context handle。"""

    def __init__(self, progress_log: list, text_log: list):
        self._progress_log = progress_log
        self._text_log = text_log

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def update(self, metrics):
        self._progress_log.append(dict(metrics))

    def on_text(self, delta):
        self._text_log.append(delta)


# ── 编排模式委托(orchestrate=True)─────────────────────────────────────────────


def _plan_json(*tasks):
    import json
    return ModelResponse(
        text=json.dumps({"tasks": list(tasks)}, ensure_ascii=False),
        tool_calls=[], usage={"input_tokens": 4, "output_tokens": 2},
        provider_payload=[],
    )


def test_orchestrated_chat_delegates_and_copies_stats():
    """orchestrate=True 时,chat 委托 Orchestrator,并把 run 统计拷回 session。"""
    from app.agent.events import RUN_COMPLETED
    router = FakeRouter([
        _plan_json({"id": "t1", "content": "唯一任务", "dependencies": []}),
        _resp_text("做完了", in_tokens=6, out_tokens=4),
    ])
    seen: list = []
    session = AgentSession(
        llm=router, tools=_registry_with(_echo_tool()),
        orchestrate=True, on_run_event=seen.append,
    )
    answer = session.chat("帮我做点事")
    assert answer == "做完了"
    # 发了 run_completed;统计被拷回 session
    assert any(e.kind == RUN_COMPLETED for e in seen)
    assert session.last_turn_usage["output_tokens"] > 0
    assert session.last_run_status == "completed"
    assert session.last_goal == "帮我做点事"


def test_orchestrated_chat_cancel_stops_run():
    """传入已取消的 CancelToken,编排路径在规划前就干净退出。"""
    from app.agent.cancel import CancelToken
    router = FakeRouter([_plan_json({"id": "t1", "content": "x"})])
    session = AgentSession(llm=router, tools=_registry_with(_echo_tool()), orchestrate=True)
    token = CancelToken()
    token.cancel()
    answer = session.chat("做事", cancel=token)
    assert answer == "已取消。"
    assert session.last_run_status == "cancelled"


def test_legacy_chat_unaffected_by_orchestrate_flag_default():
    """默认 orchestrate=False:仍走单轮循环,行为与历史一致。"""
    router = FakeRouter([_resp_text("hi")])
    session = AgentSession(llm=router, tools=_registry_with(_echo_tool()))
    assert session.chat("hello") == "hi"
    # legacy 路径不写 run 状态
    assert session.last_run_status == ""
