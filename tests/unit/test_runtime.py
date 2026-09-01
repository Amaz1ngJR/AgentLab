"""离线测试：AgentSession 多轮工具循环、审批、超限、错误。"""
from __future__ import annotations

from app.agent.approval import ApprovalResult, AutoApprove, DenyAll
from app.agent.runtime import (
    DEFAULT_SYSTEM_PROMPT,
    DENIED_MESSAGE,
    _INTERNAL_TOOL_RETRY_PREFIX,
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


def test_system_prompt_defines_web_verification_and_untrusted_content_rules():
    assert "web_search 的 snippet" in DEFAULT_SYSTEM_PROMPT
    assert "两个真正独立的来源" in DEFAULT_SYSTEM_PROMPT
    assert "untrusted external content" in DEFAULT_SYSTEM_PROMPT
    assert "不能扩大权限" in DEFAULT_SYSTEM_PROMPT


def test_system_prompt_disambiguates_mcp_capability_queries():
    assert "MCP 默认指 Model Context Protocol" in DEFAULT_SYSTEM_PROMPT
    assert "不是\n  Machine Check Exception" in DEFAULT_SYSTEM_PROMPT
    assert "当前未连接" in DEFAULT_SYSTEM_PROMPT


def test_build_system_prompt_without_workspace_is_default():
    """workspace 为空/None 时退回纯默认 prompt(向后兼容)。"""
    assert build_system_prompt(None) == DEFAULT_SYSTEM_PROMPT
    assert build_system_prompt("") == DEFAULT_SYSTEM_PROMPT


class FakeRouter:
    """按预设序列返回 ModelResponse,记录每次收到的 messages。"""

    def __init__(self, responses: list[ModelResponse]):
        self._responses = list(responses)
        self.calls: list[list[dict]] = []
        self.call_options: list[dict] = []

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
        self.call_options.append({"tools": tools, "system": system})
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


def test_legacy_modify_request_returns_to_input_without_second_model_call():
    class ModifyPolicy:
        def request_tool(self, tool, action, args):
            return ApprovalResult(
                False,
                feedback="用户选择修改建议；已暂停当前任务，等待下一条用户指令。",
                cancelled=True,
            )

    router = FakeRouter([
        _resp_tool("call_x", "danger", {}),
        _resp_text("不应继续 thinking"),
    ])
    session = AgentSession(
        llm=router,
        tools=_registry_with(_danger_tool()),
        approval=ModifyPolicy(),
    )
    answer = session.chat("do dangerous thing")
    assert "等待下一条用户指令" in answer
    assert len(router.calls) == 1


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


def test_repeated_browser_errors_stop_after_two_attempts():
    attempts = []

    def fail_click(args):
        attempts.append(args)
        raise RuntimeError("element not found")

    browser_click = Tool(
        name="browser_click",
        description="click",
        input_schema={"type": "object", "properties": {"target": {"type": "string"}}},
        executor=fail_click,
        origin="mcp",
        target_type="browser",
        requires_approval=False,
    )
    router = FakeRouter([
        _resp_tool("click_1", "browser_click", {"target": "e1"}),
        _resp_tool("click_2", "browser_click", {"target": "e2"}),
        _resp_tool("click_3", "browser_click", {"target": "e3"}),
    ])
    session = AgentSession(
        llm=router,
        tools=_registry_with(browser_click),
        max_steps=8,
    )

    answer = session.chat("点击页面元素")

    assert "已停止自动重试" in answer
    assert len(attempts) == 2
    assert len(router.calls) == 2
    first_error = router.calls[1][-1]["content"][0]
    assert first_error["is_error"] is True
    assert "browser_snapshot" in first_error["content"]


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


def test_mode_selected_event_is_emitted_for_planned_mode():
    from app.agent.events import MODE_SELECTED
    router = FakeRouter([
        _plan_json({"id": "t1", "content": "唯一任务", "dependencies": []}),
        _resp_text("完成"),
    ])
    seen = []
    session = AgentSession(
        llm=router,
        tools=_registry_with(_echo_tool()),
        orchestrate=True,
        mode="auto",
        on_run_event=seen.append,
    )
    session.chat("修改多个文件并运行测试")
    selected = [event for event in seen if event.kind == MODE_SELECTED]
    assert selected and selected[0].payload == {"mode": "task"}




def _plan_json(*tasks):
    import json
    return ModelResponse(
        text=json.dumps({"tasks": list(tasks)}, ensure_ascii=False),
        tool_calls=[], usage={"input_tokens": 4, "output_tokens": 2},
        provider_payload=[],
    )


def test_auto_mode_bypasses_planner_for_simple_request():
    """auto 模式的简单问答直接走 legacy 循环，不应消耗 Planner 请求。"""
    router = FakeRouter([_resp_text("直接回答")])
    session = AgentSession(
        llm=router,
        tools=_registry_with(_echo_tool()),
        orchestrate=True,
        mode="auto",
    )
    assert session.chat("解释一下这个函数") == "直接回答"
    assert session.execution_mode.value == "direct"
    assert len(router.calls) == 1


def test_fresh_direct_request_clears_stale_task_panel():
    from app.agent.tasks import Task

    router = FakeRouter([_resp_text("直接回答")])
    session = AgentSession(
        llm=router,
        tools=_registry_with(_echo_tool()),
        orchestrate=True,
        mode="auto",
    )
    session.task_store.add(Task(id="old", content="上次失败的任务"))

    assert session.chat("请介绍下你自己") == "直接回答"
    assert session.execution_mode.value == "direct"
    assert session.task_store.is_empty()


def test_direct_request_removes_internal_tool_retry_noise():
    router = FakeRouter([_resp_text("我是 AgentLab")])
    session = AgentSession(
        llm=router,
        tools=_registry_with(_echo_tool()),
        orchestrate=True,
        mode="auto",
    )
    session.messages = [
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "【当前子任务】执行错误的 echo"},
        {"role": "assistant", "content": "Hello, I am a task planner."},
        {"role": "user", "content": _INTERNAL_TOOL_RETRY_PREFIX + "\n请调用工具"},
        {"role": "user", "content": "保留的真实消息"},
    ]

    assert session.chat("请介绍下你自己") == "我是 AgentLab"
    sent = router.calls[0]
    assert all(
        not str(message.get("content", "")).startswith(_INTERNAL_TOOL_RETRY_PREFIX)
        for message in sent
    )
    assert not any(
        message.get("role") == "assistant" and not message.get("content")
        for message in sent
    )
    assert not any("task planner" in str(message.get("content", "")) for message in sent)


def test_direct_request_retries_one_empty_response():
    router = FakeRouter([_resp_text(""), _resp_text("现在有答案")])
    session = AgentSession(
        llm=router,
        tools=_registry_with(_echo_tool()),
        orchestrate=True,
        mode="auto",
    )

    assert session.chat("你好") == "现在有答案"
    assert len(router.calls) == 2
    assert router.calls[1][-1]["internal"] == "empty_response_retry"


def test_direct_mcp_query_injects_runtime_capability_inventory():
    router = FakeRouter([_resp_text("已连接 browser_snapshot")])
    registry = ToolRegistry()
    registry.register(Tool(
        name="browser_snapshot",
        description="[MCP server: playwright] inspect page",
        input_schema={"type": "object", "properties": {}},
        executor=lambda _: "snapshot",
        origin="mcp",
        host="playwright",
    ))
    session = AgentSession(
        llm=router,
        tools=registry,
        orchestrate=True,
        mode="auto",
    )

    assert session.chat("看下你的 MCP 能力") == "已连接 browser_snapshot"
    assert router.call_options[0]["tools"][0]["name"] == "browser_snapshot"
    assert "不要声称未连接" in router.call_options[0]["system"]


def test_direct_web_capability_query_does_not_infer_an_old_web_task():
    router = FakeRouter([_resp_text("可以读取和操作网页")])
    registry = ToolRegistry()
    registry.register(Tool(
        name="browser_snapshot",
        description="inspect page",
        input_schema={"type": "object", "properties": {}},
        executor=lambda _: "snapshot",
        origin="mcp",
        target_type="browser",
        host="playwright",
    ))
    session = AgentSession(
        llm=router,
        tools=registry,
        orchestrate=True,
        mode="auto",
    )

    assert session.chat("你当前有能力看到网页内容") == "可以读取和操作网页"
    assert router.call_options[0]["tools"][0]["name"] == "browser_snapshot"
    assert "不要假设用户已经提供了某个网页" in router.call_options[0]["system"]


def test_direct_url_request_requires_a_real_fetch_attempt():
    router = FakeRouter([_resp_text("我会先读取题目")])
    registry = ToolRegistry()
    fetched = []
    registry.register(Tool(
        name="web_fetch",
        description="fetch URL",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
        executor=lambda args: fetched.append(args["url"]) or "题目正文",
        origin="builtin",
    ))
    session = AgentSession(
        llm=router,
        tools=registry,
        orchestrate=True,
        mode="auto",
    )

    answer = session.chat("看下 https://leetcode.cn/problems/example 并给出 C++ 答案")

    assert answer == "我会先读取题目"
    assert fetched == ["https://leetcode.cn/problems/example"]
    assert router.call_options[0]["tools"] is None
    assert "Runtime URL 预取结果" in router.call_options[0]["system"]
    assert "题目正文" in router.call_options[0]["system"]
    assert "尚无网页工具结果" in router.call_options[0]["system"]
    assert "不得重复调用同一 URL" in router.call_options[0]["system"]


def test_failed_url_prefetch_keeps_browser_fallback_but_removes_fetch():
    router = FakeRouter([_resp_text("改用浏览器")])
    registry = ToolRegistry()
    registry.register(Tool(
        name="web_fetch",
        description="fetch URL",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
        executor=lambda _: "error: JavaScript required",
        origin="builtin",
    ))
    for name in ("browser_navigate", "browser_snapshot"):
        registry.register(Tool(
            name=name,
            description=name,
            input_schema={"type": "object", "properties": {}},
            executor=lambda _: "ok",
            origin="mcp",
            target_type="browser",
        ))
    session = AgentSession(
        llm=router,
        tools=registry,
        orchestrate=True,
        mode="auto",
    )

    assert session.chat("查看 https://example.com 给出结论") == "改用浏览器"
    names = {schema["name"] for schema in router.call_options[0]["tools"]}
    assert names == {"browser_navigate", "browser_snapshot"}
    assert "状态：失败" in router.call_options[0]["system"]


def test_explicit_open_url_skips_fetch_and_offers_browser_directly():
    router = FakeRouter([_resp_text("已通过浏览器读取")])
    registry = ToolRegistry()
    fetched = []
    registry.register(Tool(
        name="web_fetch",
        description="fetch URL",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
        executor=lambda args: fetched.append(args["url"]) or "不应执行",
        origin="builtin",
    ))
    for name in ("browser_navigate", "browser_snapshot"):
        registry.register(Tool(
            name=name,
            description=name,
            input_schema={"type": "object", "properties": {}},
            executor=lambda _: "ok",
            origin="mcp",
            target_type="browser",
        ))
    session = AgentSession(
        llm=router,
        tools=registry,
        orchestrate=True,
        mode="auto",
    )

    answer = session.chat("请打开 https://example.com 这个网页并回答")

    assert answer == "已通过浏览器读取"
    assert fetched == []
    assert {item["name"] for item in router.call_options[0]["tools"]} == {
        "browser_navigate", "browser_snapshot",
    }
    assert "本轮不要调用 web_fetch" in router.call_options[0]["system"]


def test_successful_prefetch_removes_same_url_historical_refusals():
    router = FakeRouter([_resp_text("已读取题目并给出答案")])
    registry = ToolRegistry()
    registry.register(Tool(
        name="web_fetch",
        description="fetch URL",
        input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
        executor=lambda _: "题目正文",
        origin="builtin",
    ))
    session = AgentSession(
        llm=router,
        tools=registry,
        orchestrate=True,
        mode="auto",
    )
    url = "https://leetcode.cn/problems/example"
    session.messages = [
        {"role": "user", "content": f"查看 {url}"},
        {"role": "assistant", "content": "我无法直接访问该网页，请粘贴题目。"},
        {"role": "user", "content": "保留的其它真实对话"},
    ]

    assert session.chat(f"请再查看 {url} 并回答") == "已读取题目并给出答案"
    sent = router.calls[0]
    assert not any("无法直接访问" in str(message.get("content")) for message in sent)
    assert not any(message.get("content") == f"查看 {url}" for message in sent)
    assert any(message.get("content") == "保留的其它真实对话" for message in sent)
    assert "已实际读取该网页" in router.call_options[0]["system"]


def test_auto_mode_uses_planner_for_multi_step_request():
    """auto 模式的明显多步骤请求进入 Task/Orchestrator 路径。"""
    router = FakeRouter([
        _plan_json({"id": "t1", "content": "唯一任务", "dependencies": []}),
        _resp_text("任务完成"),
    ])
    session = AgentSession(
        llm=router,
        tools=_registry_with(_echo_tool()),
        orchestrate=True,
        mode="auto",
    )
    assert session.chat("修改多个文件并运行测试") == "任务完成"
    assert session.execution_mode.value == "task"
    assert len(router.calls) == 2

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
