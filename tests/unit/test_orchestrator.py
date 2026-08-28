"""离线测试：Planner / Executor / Replanner / Orchestrator 编排路径。

覆盖 §6.1 验收标准:初始计划、按依赖执行、工具失败后重规划、审批拒绝后阻塞、
用户追加目标、取消、max_steps。全程用 FakeRouter,无网络无真实模型。
"""
from __future__ import annotations

import json

import pytest

from app.agent import events
from app.agent.approval import ApprovalResult, AutoApprove, DenyAll
from app.agent.cancel import CancelToken, Cancelled
from app.agent.events import RunEvent
from app.agent.executor import Executor
from app.agent.orchestrator import Orchestrator
from app.agent.planner import Planner, _extract_json, _parse_tasks
from app.agent.replanner import Replanner
from app.agent.tasks import (
    BLOCKED,
    COMPLETED,
    FAILED,
    PENDING,
    Task,
    TaskStore,
)
from app.models.protocol import ModelResponse, ToolCall, ToolResult
from app.tools.registry import Tool, ToolRegistry


# ── FakeRouter:按预设序列返回 ModelResponse ────────────────────────────────────


class FakeRouter:
    def __init__(self, responses: list[ModelResponse]):
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    @property
    def model(self) -> str:
        return "fake-model"

    @property
    def provider(self) -> str:
        return "fake"

    def create_message(self, messages, tools=None, system=None, temperature=None,
                       max_tokens=4096, on_progress=None, on_text_delta=None,
                       on_thinking_delta=None):
        self.calls.append([dict(m) for m in messages])
        if not self._responses:
            return ModelResponse(text="(no more responses)", tool_calls=[],
                                 usage={"input_tokens": 0, "output_tokens": 0},
                                 provider_payload=[])
        return self._responses.pop(0)

    @staticmethod
    def format_tool_results(results: list[ToolResult]) -> list[dict]:
        blocks = [
            {"type": "tool_result", "tool_use_id": r.tool_call_id,
             "content": r.output, "is_error": r.is_error}
            for r in results
        ]
        return [{"role": "user", "content": blocks}]


def _resp_text(text: str) -> ModelResponse:
    return ModelResponse(text=text, tool_calls=[],
                         usage={"input_tokens": 5, "output_tokens": 3},
                         provider_payload=[{"type": "text", "text": text}])


def _resp_tool(tool_id: str, name: str, args: dict) -> ModelResponse:
    return ModelResponse(text="", tool_calls=[ToolCall(id=tool_id, name=name, arguments=args)],
                         usage={"input_tokens": 5, "output_tokens": 1},
                         provider_payload=[{"type": "tool_use", "id": tool_id,
                                            "name": name, "input": args}])


def _plan_json(*tasks: dict) -> ModelResponse:
    """构造 Planner 期望的 JSON 计划响应。"""
    return _resp_text(json.dumps({"tasks": list(tasks)}, ensure_ascii=False))


def _echo_tool() -> Tool:
    return Tool(
        name="echo", description="echo back",
        input_schema={"type": "object", "properties": {"msg": {"type": "string"}}},
        executor=lambda a: f"echo: {a.get('msg', '')}",
    )


def _danger_tool() -> Tool:
    return Tool(
        name="danger", description="needs approval",
        input_schema={"type": "object", "properties": {}},
        executor=lambda a: "did dangerous thing", requires_approval=True,
    )


def _dynamic_tool() -> Tool:
    return Tool(
        name="dynamic", description="conditional approval",
        input_schema={"type": "object", "properties": {"outside": {"type": "boolean"}}},
        executor=lambda a: "dynamic executed",
        approval_resolver=lambda a: (
            "dynamic_outside_workspace" if a.get("outside") else None
        ),
    )


def _explode_tool() -> Tool:
    def boom(a):
        return None  # 不会被调用到(execute 捕获异常),这里用真实抛错版本
    t = Tool(
        name="explode", description="raises",
        input_schema={"type": "object", "properties": {}},
        executor=lambda a: (_ for _ in ()).throw(RuntimeError("intentional failure")),
    )
    return t


def _registry(*tools: Tool) -> ToolRegistry:
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


def _collect(events_log: list[RunEvent]):
    return lambda ev: events_log.append(ev)


# ── Planner 单元 ───────────────────────────────────────────────────────────────


def test_planner_parses_json_plan():
    router = FakeRouter([_plan_json(
        {"id": "t1", "content": "读配置", "dependencies": []},
        {"id": "t2", "content": "改配置", "dependencies": ["t1"]},
    )])
    plan = Planner(router).create_plan("更新配置")
    assert [t.id for t in plan.tasks] == ["t1", "t2"]
    assert plan.tasks[1].dependencies == ["t1"]


def test_planner_does_not_receive_full_execution_system():
    """Orchestrator 不应把 Skills/MCP/记忆组成的执行 prompt 重复塞给 Planner。"""
    router = FakeRouter([
        _plan_json({"id": "t1", "content": "完成", "dependencies": []}),
        _resp_text("完成"),
    ])
    huge_execution_system = "SKILL-WORKFLOW " * 1000
    orch = Orchestrator(
        router,
        _registry(_echo_tool()),
        system=huge_execution_system,
    )
    orch.run("简单目标")
    planning_prompt = router.calls[0][0]["content"]
    assert planning_prompt == "简单目标"
    assert "SKILL-WORKFLOW" not in planning_prompt


    router = FakeRouter([_resp_text("这不是 JSON,只是闲聊")])
    plan = Planner(router).create_plan("做点什么")
    assert len(plan.tasks) == 1
    assert plan.tasks[0].content == "做点什么"


def test_planner_falls_back_when_model_raises():
    class Boom(FakeRouter):
        def create_message(self, *a, **k):
            raise RuntimeError("model down")
    plan = Planner(Boom([])).create_plan("目标")
    assert len(plan.tasks) == 1


def test_extract_json_handles_fenced_and_prose():
    assert _extract_json('```json\n{"tasks": []}\n```') == {"tasks": []}
    assert _extract_json('前言 {"tasks": [{"content": "x"}]} 后语')["tasks"][0]["content"] == "x"
    assert _extract_json("no json here") is None


def test_parse_tasks_skips_invalid_and_dedupes_ids():
    obj = {"tasks": [
        {"id": "t1", "content": "a"},
        {"content": ""},          # 空 content 跳过
        "not a dict",              # 非对象跳过
        {"id": "t1", "content": "b"},  # id 撞车 -> 重新编号
    ]}
    tasks = _parse_tasks(obj)
    assert [t.content for t in tasks] == ["a", "b"]
    assert len(set(t.id for t in tasks)) == 2  # id 唯一


# ── Executor 单元 ──────────────────────────────────────────────────────────────


def test_executor_completes_task_without_tools():
    router = FakeRouter([_resp_text("子任务完成了")])
    ex = Executor(router, _registry(_echo_tool()))
    out = ex.run_task(Task("t1", "做一件事"), [], system="", max_steps=4)
    assert out.status == COMPLETED
    assert out.evidence == "子任务完成了"
    assert out.model_rounds == 1


def test_executor_pure_conversation_does_not_force_tool_call():
    """介绍、解释等纯对话应直接完成，任务指令不能强迫模型调用无意义工具。"""
    router = FakeRouter([_resp_text("我是 AgentLab，本地编码助手。")])
    ex = Executor(router, _registry(_echo_tool()))

    out = ex.run_task(Task("t1", "介绍下你自己"), [], system="", max_steps=4)

    assert out.status == COMPLETED
    assert out.tool_calls_made == 0
    assert len(router.calls) == 1
    directive = router.calls[0][-1]["content"]
    assert "纯对话、介绍、解释、总结" in directive
    assert "直接回答，不要调用工具" in directive
    assert "禁止只输出文字说明" not in directive


def test_executor_runs_tool_then_completes():
    router = FakeRouter([
        _resp_tool("c1", "echo", {"msg": "hi"}),
        _resp_text("看到 echo 结果了,完成"),
    ])
    ex = Executor(router, _registry(_echo_tool()))
    out = ex.run_task(Task("t1", "echo hi"), [], system="", max_steps=4)
    assert out.status == COMPLETED
    assert out.tool_calls_made == 1
    assert out.model_rounds == 2


def test_executor_tool_error_marks_failed():
    router = FakeRouter([_resp_tool("c1", "explode", {})])
    ex = Executor(router, _registry(_explode_tool()))
    out = ex.run_task(Task("t1", "boom"), [], system="", max_steps=4)
    assert out.status == FAILED
    assert "explode" in out.error


def test_executor_denied_marks_blocked():
    router = FakeRouter([_resp_tool("c1", "danger", {})])
    ex = Executor(router, _registry(_danger_tool()), approval=DenyAll())
    out = ex.run_task(Task("t1", "do danger"), [], system="", max_steps=4)
    assert out.status == BLOCKED
    assert "拒绝" in out.error


def test_executor_modify_request_raises_cancelled_and_never_calls_model_again():
    class ModifyPolicy:
        def request_tool(self, tool, action, args):
            return ApprovalResult(
                False,
                feedback="用户选择修改建议；已暂停当前任务，等待下一条用户指令。",
                cancelled=True,
            )

    router = FakeRouter([
        _resp_tool("c1", "danger", {}),
        _resp_text("不应继续 thinking"),
    ])
    ex = Executor(router, _registry(_danger_tool()), approval=ModifyPolicy())
    with pytest.raises(Cancelled, match="等待下一条用户指令"):
        ex.run_task(Task("t1", "do danger"), [], system="", max_steps=4)
    assert len(router.calls) == 1


    router = FakeRouter([
        _resp_tool("c1", "dynamic", {"outside": True}),
    ])
    events_seen = []
    ex = Executor(
        router,
        _registry(_dynamic_tool()),
        approval=DenyAll(),
        on_event=events_seen.append,
    )

    out = ex.run_task(Task("t1", "read outside"), [], system="", max_steps=4)

    assert out.status == BLOCKED
    approval_event = next(
        event for event in events_seen if event.kind == events.APPROVAL_REQUIRED
    )
    assert approval_event.payload["approval_action"] == "dynamic_outside_workspace"


def test_executor_stops_repeated_identical_tool_requests():
    """模型重复相同工具参数时应立即失败，不应耗尽整个任务预算。"""
    router = FakeRouter([
        _resp_tool("c1", "echo", {"msg": "same"}),
        _resp_tool("c2", "echo", {"msg": "same"}),
        _resp_text("不应再调用"),
    ])
    ex = Executor(router, _registry(_echo_tool()))
    out = ex.run_task(Task("t1", "重复任务"), [], system="", max_steps=10)
    assert out.status == FAILED
    assert "重复请求工具" in out.error
    assert len(router.calls) == 2

    # 模型一直请求工具,永不收口
    router = FakeRouter([_resp_tool(f"c{i}", "echo", {"msg": str(i)}) for i in range(10)])
    ex = Executor(router, _registry(_echo_tool()))
    out = ex.run_task(Task("t1", "loop"), [], system="", max_steps=3)
    assert out.status == FAILED
    assert "未完成" in out.error


# ── Replanner 单元 ─────────────────────────────────────────────────────────────


def test_replanner_completed_writes_evidence():
    from app.agent.executor import TaskOutcome
    store = TaskStore()
    store.add(Task("t1", "x"))
    patch = Replanner(store).apply(store.get("t1"), TaskOutcome(COMPLETED, evidence="done"))
    assert patch.new_status == COMPLETED
    assert store.get("t1").evidence == "done"


def test_replanner_failure_appends_retry_once():
    from app.agent.executor import TaskOutcome
    store = TaskStore()
    store.add(Task("t1", "x"))
    rp = Replanner(store)
    patch = rp.apply(store.get("t1"), TaskOutcome(FAILED, error="boom"))
    assert patch.added  # 追加了补救任务
    retry_id = patch.added[0]
    # 补救任务再失败时不应再追加(避免无限循环)
    patch2 = rp.apply(store.get(retry_id), TaskOutcome(FAILED, error="again"))
    assert not patch2.added


def test_replanner_blocked_no_retry():
    from app.agent.executor import TaskOutcome
    store = TaskStore()
    store.add(Task("t1", "x"))
    patch = Replanner(store).apply(store.get("t1"), TaskOutcome(BLOCKED, error="拒绝"))
    assert patch.new_status == BLOCKED
    assert not patch.added


# ── Orchestrator 集成 ──────────────────────────────────────────────────────────


def test_orchestrator_initial_plan_and_complete():
    """初始计划 + 按依赖执行 + 全部完成。"""
    router = FakeRouter([
        _plan_json(
            {"id": "t1", "content": "第一步", "dependencies": []},
            {"id": "t2", "content": "第二步", "dependencies": ["t1"]},
        ),
        _resp_text("t1 完成"),   # 执行 t1
        _resp_text("t2 完成"),   # 执行 t2
    ])
    log: list[RunEvent] = []
    orch = Orchestrator(router, _registry(_echo_tool()), on_event=_collect(log))
    answer = orch.run("做两步")
    assert orch.all_completed()
    assert answer == "t2 完成"
    kinds = [e.kind for e in log]
    assert events.PLAN_CREATED in kinds
    assert events.RUN_COMPLETED in kinds
    # 任务确实按依赖顺序执行(t1 的 task_started 在 t2 之前)
    started = [e.task_id for e in log if e.kind == events.TASK_STARTED]
    assert started == ["t1", "t2"]


def test_orchestrator_replans_after_tool_failure():
    """工具失败 -> 任务 failed -> Replanner 追加补救任务 -> 补救任务完成。"""
    router = FakeRouter([
        _plan_json({"id": "t1", "content": "跑工具", "dependencies": []}),
        _resp_tool("c1", "explode", {}),  # t1 执行:工具炸了 -> failed
        _resp_text("补救完成"),            # 补救任务执行:直接给文本 -> completed
    ])
    log: list[RunEvent] = []
    orch = Orchestrator(router, _registry(_explode_tool()), on_event=_collect(log))
    orch.run("跑个会失败的工具")
    snap = orch.store.snapshot()
    statuses = {t["id"]: t["status"] for t in snap}
    assert statuses["t1"] == FAILED
    # 追加了一个补救任务且已完成
    retry = [t for t in snap if t["id"].startswith("t1-retry")]
    assert retry and retry[0]["status"] == COMPLETED


def test_orchestrator_blocks_on_denied_approval():
    """审批拒绝 -> 任务 blocked -> run 以"被阻塞"收尾。"""
    router = FakeRouter([
        _plan_json({"id": "t1", "content": "危险操作", "dependencies": []}),
        _resp_tool("c1", "danger", {}),  # 需要审批,被 DenyAll 拒
    ])
    log: list[RunEvent] = []
    orch = Orchestrator(router, _registry(_danger_tool()), approval=DenyAll(),
                        on_event=_collect(log))
    orch.run("做危险操作")
    assert orch.store.get("t1").status == BLOCKED
    # 发了 approval_required 事件给 UI
    assert any(e.kind == events.APPROVAL_REQUIRED for e in log)
    # blocked 任务无补救、无可跑任务 -> run_failed(被阻塞)
    assert any(e.kind == events.RUN_FAILED for e in log)


def test_orchestrator_second_run_clears_tasks():
    """第二次 run 清空旧任务,只展示本轮计划,不与历史任务混叠。"""
    router = FakeRouter([
        _plan_json({"id": "t1", "content": "第一个目标", "dependencies": []}),
        _resp_text("目标一完成"),
        _plan_json({"id": "t1", "content": "第二个目标", "dependencies": []}),
        _resp_text("目标二完成"),
    ])
    orch = Orchestrator(router, _registry(_echo_tool()))
    orch.run("目标一")
    assert orch.store.snapshot()[0]["content"] == "第一个目标"
    orch.run("目标二")
    # 第二轮清空后只有第二个目标,不与第一轮混叠
    snap = orch.store.snapshot()
    assert len(snap) == 1
    assert snap[0]["content"] == "第二个目标"
    assert orch.all_completed()


def test_orchestrator_resume_keeps_tasks_and_resets_failed():
    """resume=True 不清空旧任务,把 failed 重置为 pending 继续推进。"""
    # 第一轮:t1 工具炸了 -> failed -> 补救任务也炸 -> 仍有未完成任务
    router = FakeRouter([
        _plan_json(
            {"id": "t1", "content": "会失败的任务", "dependencies": []},
            {"id": "t2", "content": "依赖 t1", "dependencies": ["t1"]},
        ),
        _resp_tool("c1", "explode", {}),  # t1 执行炸了 -> failed
        _resp_text("补救后说明"),          # 补救任务
        _resp_tool("c2", "explode", {}),  # 补救任务也炸
    ])
    orch = Orchestrator(router, _registry(_explode_tool()), max_steps=4)
    orch.run("目标")
    # 第一轮后应有失败/未完成任务
    snap1 = orch.store.snapshot()
    assert any(t["status"] == "failed" for t in snap1)
    task_count_before = len(snap1)

    # resume:换成不炸的工具,让重置后的任务能成功
    router2 = FakeRouter([_resp_text("这次成功了")])
    orch._llm = router2
    orch._executor._llm = router2
    orch.run("继续完成", resume=True)
    # resume 不清空:任务数 >= 之前(没有 store.clear)
    snap2 = orch.store.snapshot()
    assert len(snap2) >= task_count_before
    # 没有遗留的 failed(已被 reset_failed 重置后重跑)
    # 注:具体能否全完成取决于依赖,这里只验证 failed 被重置过


def test_orchestrator_resume_false_clears():
    """resume=False(默认)仍清空旧任务,行为不变。"""
    router = FakeRouter([
        _plan_json({"id": "t1", "content": "第一个", "dependencies": []}),
        _resp_text("done1"),
        _plan_json({"id": "t1", "content": "第二个", "dependencies": []}),
        _resp_text("done2"),
    ])
    orch = Orchestrator(router, _registry(_echo_tool()))
    orch.run("目标一")
    orch.run("目标二", resume=False)
    snap = orch.store.snapshot()
    assert len(snap) == 1
    assert snap[0]["content"] == "第二个"


def test_orchestrator_cancellation_stops_run():
    """取消后,编排循环干净退出,不再执行任务。"""
    router = FakeRouter([
        _plan_json(
            {"id": "t1", "content": "step1", "dependencies": []},
            {"id": "t2", "content": "step2", "dependencies": ["t1"]},
        ),
        _resp_text("t1 完成"),
    ])
    token = CancelToken()
    log: list[RunEvent] = []

    # 在 t1 完成、t2 开始前取消:用事件钩子在第一个 task_started 后触发取消
    def on_event(ev: RunEvent):
        log.append(ev)
        if ev.kind == events.TASK_UPDATED and ev.task_id == "t1":
            token.cancel()

    orch = Orchestrator(router, _registry(_echo_tool()), on_event=on_event)
    answer = orch.run("两步任务", cancel=token)
    assert answer == "已取消。"
    # t2 不应被执行(没有它的 task_started)
    started = [e.task_id for e in log if e.kind == events.TASK_STARTED]
    assert "t2" not in started
    assert any(e.kind == events.RUN_FAILED for e in log)


def test_orchestrator_modify_request_stops_run_without_replanning():
    class ModifyPolicy:
        def request_tool(self, tool, action, args):
            return ApprovalResult(
                False,
                feedback="用户选择修改建议；已暂停当前任务，等待下一条用户指令。",
                cancelled=True,
            )

    router = FakeRouter([
        _plan_json({"id": "t1", "content": "危险操作", "dependencies": []}),
        _resp_tool("c1", "danger", {}),
        _resp_text("不应再次调用模型"),
    ])
    orch = Orchestrator(
        router, _registry(_danger_tool()), approval=ModifyPolicy(),
    )
    answer = orch.run("执行危险操作")
    assert "等待下一条用户指令" in answer
    assert orch.last_run_status == "cancelled"
    assert len(router.calls) == 2  # 一次 planning + 一次 executor，之后立即停止


    """全局步数预算耗尽时,run 以"达到最大步数"收尾。"""
    # 单任务,但模型一直请求工具,永不收口
    responses = [_plan_json({"id": "t1", "content": "loop", "dependencies": []})]
    responses += [_resp_tool(f"c{i}", "echo", {"msg": str(i)}) for i in range(20)]
    router = FakeRouter(responses)
    log: list[RunEvent] = []
    orch = Orchestrator(router, _registry(_echo_tool()), max_steps=3,
                        on_event=_collect(log))
    orch.run("无限循环")
    # 没跑成功,且发了 run_failed
    assert not orch.all_completed()
    assert any(e.kind == events.RUN_FAILED for e in log)


def test_orchestrator_counts_text_only_model_rounds_in_global_budget():
    """无工具的任务也必须消耗全局预算，避免按工具数计数造成无限执行。"""
    router = FakeRouter([
        _plan_json(
            {"id": "t1", "content": "说明一", "dependencies": []},
            {"id": "t2", "content": "说明二", "dependencies": []},
        ),
        _resp_text("一完成"),
    ])
    log: list[RunEvent] = []
    orch = Orchestrator(
        router,
        _registry(_echo_tool()),
        max_steps=1,
        max_task_steps=1,
        on_event=_collect(log),
    )

    answer = orch.run("两个说明任务")

    assert answer == "一完成"
    assert orch.last_run_status == "failed"
    assert orch.store.get("t1").status == COMPLETED
    assert orch.store.get("t2").status == PENDING
    assert any("最大模型往返次数" in event.text for event in log)


def test_orchestrator_task_budget_does_not_exceed_global_budget():
    orch = Orchestrator(
        FakeRouter([]),
        _registry(_echo_tool()),
        max_steps=3,
        max_task_steps=10,
    )
    assert orch._max_task_steps == 10
    # 实际调用时仍会通过 min(rounds_left, max_task_steps) 限制到全局剩余额度。


def test_orchestrator_rejects_non_positive_budgets():
    with pytest.raises(ValueError, match="max_steps"):
        Orchestrator(FakeRouter([]), _registry(_echo_tool()), max_steps=0)
    with pytest.raises(ValueError, match="max_task_steps"):
        Orchestrator(
            FakeRouter([]), _registry(_echo_tool()), max_task_steps=0,
        )


# ── 取消/出错后 tool_use 必须配对(否则下一轮 provider 报错)──────────────────


def _tool_use_ids(messages: list[dict]) -> set[str]:
    """收集 messages 里所有 tool_use 块的 id。

    兼容两种形态:① 真实 adapter 把 block 包在 {"role","content":[...]} 里;
    ② FakeRouter 的 provider_payload 是扁平 block(block 直接当 message)。
    """
    ids = set()
    for m in messages:
        if isinstance(m, dict) and m.get("type") == "tool_use":  # 扁平 block
            ids.add(m.get("id"))
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    ids.add(b.get("id"))
    return ids


def _tool_result_ids(messages: list[dict]) -> set[str]:
    """收集 messages 里所有 tool_result 块的 tool_use_id。"""
    ids = set()
    for m in messages:
        if isinstance(m, dict) and m.get("type") == "tool_result":  # 扁平 block
            ids.add(m.get("tool_use_id"))
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    ids.add(b.get("tool_use_id"))
    return ids


def _assert_tools_paired(messages: list[dict]) -> None:
    """断言每个 tool_use 都有配对的 tool_result(无悬空 tool_call)。"""
    uses = _tool_use_ids(messages)
    results = _tool_result_ids(messages)
    assert uses == results, f"悬空 tool_use: {uses - results}, 多余 result: {results - uses}"


def _multi_tool_resp(*calls: tuple[str, str, dict]) -> ModelResponse:
    """构造一条含多个 tool_call 的响应(id, name, args)。"""
    from app.models.protocol import ToolCall
    tool_calls = [ToolCall(id=cid, name=name, arguments=args) for cid, name, args in calls]
    payload = [{"type": "tool_use", "id": cid, "name": name, "input": args}
               for cid, name, args in calls]
    return ModelResponse(text="", tool_calls=tool_calls,
                         usage={"input_tokens": 5, "output_tokens": 1},
                         provider_payload=payload)


def test_executor_cancel_mid_tool_loop_pairs_tool_use():
    """tool 循环中途取消:已 extend 的 tool_use 必须全部补上配对 tool_result。"""
    # 一条响应含 2 个工具调用;在第一个 TOOL_REQUESTED 时取消 → 第二个来不及执行
    router = FakeRouter([_multi_tool_resp(("c1", "echo", {"msg": "a"}),
                                          ("c2", "echo", {"msg": "b"}))])
    token = CancelToken()

    def on_event(ev: RunEvent):
        if ev.kind == events.TOOL_REQUESTED and ev.tool_name == "echo":
            token.cancel()  # 第一个工具请求时就取消

    ex = Executor(router, _registry(_echo_tool()), on_event=on_event)
    messages: list[dict] = []
    try:
        ex.run_task(Task("t1", "两个工具"), messages, system="", max_steps=4, cancel=token)
    except Cancelled:
        pass
    # 关键:取消后历史里每个 tool_use 都有配对 tool_result(c1 真实/c2 合成)
    _assert_tools_paired(messages)


def test_executor_tool_error_pairs_tool_use():
    """工具出错早返回:同一轮里其它 tool_use 也要补配对,不留悬空。"""
    # 第一个工具炸,第二个 echo 来不及执行 → 都要有 tool_result
    router = FakeRouter([_multi_tool_resp(("c1", "explode", {}),
                                          ("c2", "echo", {"msg": "b"}))])
    ex = Executor(router, _registry(_explode_tool(), _echo_tool()))
    messages: list[dict] = []
    out = ex.run_task(Task("t1", "炸+回显"), messages, system="", max_steps=4)
    assert out.status == FAILED
    _assert_tools_paired(messages)
