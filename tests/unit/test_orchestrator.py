"""离线测试：Planner / Executor / Replanner / Orchestrator 编排路径。

覆盖 §6.1 验收标准:初始计划、按依赖执行、工具失败后重规划、审批拒绝后阻塞、
用户追加目标、取消、max_steps。全程用 FakeRouter,无网络无真实模型。
"""
from __future__ import annotations

import json

from app.agent import events
from app.agent.approval import AutoApprove, DenyAll
from app.agent.cancel import CancelToken
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
                       max_tokens=4096, on_progress=None, on_text_delta=None):
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


def test_planner_falls_back_to_single_task_on_garbage():
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


def test_executor_runs_tool_then_completes():
    router = FakeRouter([
        _resp_tool("c1", "echo", {"msg": "hi"}),
        _resp_text("看到 echo 结果了,完成"),
    ])
    ex = Executor(router, _registry(_echo_tool()))
    out = ex.run_task(Task("t1", "echo hi"), [], system="", max_steps=4)
    assert out.status == COMPLETED
    assert out.tool_calls_made == 1


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


def test_executor_step_budget_exhausted_fails():
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


def test_orchestrator_respects_max_steps():
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
