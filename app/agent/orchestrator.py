"""Orchestrator —— 串联 Planner + Executor + Replanner + TaskStore 的编排入口。

一次 run 的流程(technical_architecture.md §7.4):
  1. Planner.create_plan(goal) 产出初始任务,写入 TaskStore -> 发 plan_created。
  2. 循环:从 TaskStore claim 下一个可执行任务 -> Executor.run_task 执行 ->
     Replanner.apply 回写状态(必要时追加补救任务)-> 发 task_updated。
  3. 直到:全部任务到终态(收工)、卡死(剩余任务依赖被 failed 卡住)、
     取消、或全局步数预算耗尽。
  4. 发 run_completed(或 run_failed),返回最终答复文本。

与 AgentSession 的分工:
  AgentSession 仍是 CLI 当前主路径(单轮工具循环)。Orchestrator 是 §6.1 要求的
  显式编排路径,持有跨任务共享的 messages,产出结构化 RunEvent,供测试和未来
  Web UI/TUI 使用。两者复用同一套 ModelRouter / ToolRegistry / ApprovalPolicy。

run(goal) 可多次调用:第二次会在已有 messages 和 TaskStore 之上追加新计划,
实现"用户中途追加目标"。TaskStore 是唯一可信任务状态源,可被 UI 随时快照。
"""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Callable, ContextManager, Optional

from app.agent import events
from app.agent.approval import ApprovalPolicy, AutoApprove
from app.agent.cancel import Cancelled, CancelToken
from app.agent.events import RunEvent
from app.agent.executor import Executor
from app.agent.planner import Planner
from app.agent.replanner import Replanner
from app.agent.tasks import COMPLETED, TaskStore
from app.models.router import ModelRouter
from app.tools.registry import ToolRegistry

ProgressFn = Callable[[str], ContextManager[Any]]


class Orchestrator:
    """编排一次(或多次)目标的规划—执行—重规划。"""

    def __init__(
        self,
        llm: ModelRouter,
        tools: ToolRegistry,
        approval: Optional[ApprovalPolicy] = None,
        system: str = "",
        *,
        max_steps: int = 12,
        max_task_steps: int | None = None,
        task_store: Optional[TaskStore] = None,
        planner: Optional[Planner] = None,
        on_event: Optional[Callable[[RunEvent], None]] = None,
        progress: Optional[ProgressFn] = None,
        messages: Optional[list[dict[str, Any]]] = None,
        context_manager: Optional[Any] = None,
    ):
        self._llm = llm
        self._tools = tools
        self._approval: ApprovalPolicy = approval or AutoApprove()
        self._system = system
        if max_steps <= 0:
            raise ValueError("max_steps 必须 > 0")
        if max_task_steps is not None and max_task_steps <= 0:
            raise ValueError("max_task_steps 必须 > 0")
        self._max_steps = max_steps
        # 单任务模型往返上限与整个 run 的全局模型往返预算分开。默认不超过
        # 全局预算，避免第一个任务独占全部额度后其他任务永远无法执行。
        self._max_task_steps = max_task_steps or min(8, max_steps)
        self.store: TaskStore = task_store or TaskStore()
        self._planner = planner or Planner(llm)
        self._replanner = Replanner(self.store)
        self._progress: ProgressFn = progress or (lambda label: nullcontext())
        self._executor = Executor(
            llm, tools, self._approval,
            on_event=self._forward,
            progress=self._progress,
            context_manager=context_manager,  # 任务内压缩钩子
        )
        self._emit = on_event or (lambda e: None)
        # 跨任务、跨 run 共享的对话历史(后做的任务能看到先做任务的上下文)。
        # 允许外部传入一个已存在的 list(AgentSession 把自己的 messages 交进来共享)。
        self.messages: list[dict[str, Any]] = messages if messages is not None else []
        # 上下文预算/压缩协调者(可选,§7.3)。None 时不做任何预算检查与压缩,
        # 行为与之前完全一致(既有测试不受影响)。
        self._ctx = context_manager
        self._run_seq = 0  # run 计数,用于给任务 id 加 run 前缀,避免跨 run 撞 id
        # run 级统计:每次 run() 重置,供 AgentSession 拷回去展示
        self.last_run_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self.last_actual_model: Optional[str] = None
        self.last_run_status: str = ""  # completed / blocked / failed / cancelled
        # 本轮 run() 累计的真实工具调用次数(供 Loop 模式累加进预算)。每次 run() 重置。
        self.last_run_tool_calls: int = 0

    def _namespace_tasks(self, tasks: list) -> list:
        """给一批新计划的任务 id 加 run 前缀,并同步重映射其 dependencies。

        Planner 每次都从 t1 开始编号,多次 run 会撞 id 被 store.add 去重丢弃。
        加上 rN- 前缀后,既保证跨 run 唯一,又保留 plan 内部的依赖关系。
        第一个 run(_run_seq==1)不加前缀,保持单 run 场景 id 简洁(t1/t2)。
        """
        if self._run_seq <= 1:
            return tasks
        prefix = f"r{self._run_seq}-"
        local_ids = {t.id for t in tasks}
        for t in tasks:
            t.dependencies = [prefix + d if d in local_ids else d for d in t.dependencies]
            t.id = prefix + t.id
        return tasks

    def _forward(self, ev: RunEvent) -> None:
        """Executor 的事件直接转发给外部 on_event。"""
        self._emit(ev)

    def _emit_task_update(self, task_id: str, status: str, note: str = "") -> None:
        self._emit(RunEvent(
            kind=events.TASK_UPDATED, task_id=task_id, task_status=status,
            text=note, payload={"tasks": self.store.snapshot()},
        ))

    def _maybe_compact(self) -> None:
        """在稳定点(规划后 / 每个任务完成后)检查上下文预算并按需压缩。

        无 context_manager 时直接返回(默认行为)。压缩就地改短 self.messages;
        切点只落在已闭合的历史里,不会破坏正在执行任务的 tool_use/tool_result 对。
        压缩异常一律吞掉:上下文压缩是兜底优化,绝不能让它中断主任务。
        """
        if self._ctx is None:
            return
        # 先预检状态:只在需要压缩时才显示 spinner,避免每个稳定点都闪 "compacting (0.0s)"
        # 的误导。context_manager 会在真正压缩时发 COMPACTION_STARTED/COMPLETED 事件,
        # UI 那边会打印压缩进度,这里就不需要无条件套 spinner 了。
        est = self._ctx.estimate(self.messages, system=self._system)
        status = self._ctx.budget.status_for(est)
        if status != "compact":
            return  # 预算还够,不需要压缩
        try:
            with self._progress("compacting") as handle:
                on_progress = getattr(handle, "update", None)
                self._ctx.maybe_compact(
                    self.messages, system=self._system, on_progress=on_progress,
                )
        except Exception:
            pass

    def run(
        self,
        goal: str,
        *,
        cancel: Optional[CancelToken] = None,
        resume: bool = False,
        append_goal_message: bool = True,
    ) -> str:
        """规划并执行一个目标,返回最终答复文本。

        cancel 用于协作式取消;不传则不可取消。
        resume=True 时不清空旧任务(继续上一轮未完成的任务,失败任务重置为 pending);
        resume=False(默认)时清空,确保任务面板只展示当前轮的计划。
        """
        cancel = cancel or CancelToken()
        self._run_seq += 1
        self.last_run_usage = {"input_tokens": 0, "output_tokens": 0}
        self.last_run_status = ""
        self.last_run_tool_calls = 0
        if not resume:
            # 非 resume 模式:清空旧任务,只展示本轮计划
            self.store.clear()
        else:
            # resume 模式:保留已有任务,把 failed 重置为 pending,继续推进
            reset_count = self.store.reset_failed()
            if reset_count > 0:
                self._emit(RunEvent(
                    kind=events.RUN_STARTED,
                    text=f"继续上一轮任务(已重置 {reset_count} 个失败任务)",
                ))
        if append_goal_message:
            self.messages.append({"role": "user", "content": goal})
        self._emit(RunEvent(kind=events.RUN_STARTED, text=goal))

        def _record_model(m: str) -> None:
            self.last_actual_model = m

        # ── 1. 规划 ──────────────────────────────────────────────────────────
        try:
            cancel.raise_if_cancelled()
            with self._progress("planning") as handle:
                on_progress = getattr(handle, "update", None)
                plan = self._planner.create_plan(
                    goal,
                    # Planner 已有独立、严格的 PLANNER_SYSTEM。把完整执行 system
                    # （含 Skills/MCP/记忆，实测可多出数千 token）再塞进 user prompt
                    # 会重复上下文，导致 planning 一开始就显示 3.9k 输入 token。
                    context="",
                    on_progress=on_progress,
                )
        except Cancelled:
            self.last_run_status = "cancelled"
            self._emit(RunEvent(kind=events.RUN_FAILED, text="已取消(规划阶段)"))
            return "已取消。"
        for k in ("input_tokens", "output_tokens"):
            self.last_run_usage[k] += self._planner.last_usage.get(k, 0)
        if self._planner.last_actual_model:
            self.last_actual_model = self._planner.last_actual_model
        self.store.extend(self._namespace_tasks(plan.tasks))
        self._emit(RunEvent(kind=events.PLAN_CREATED,
                            payload={"tasks": self.store.snapshot()}))
        # 规划后是第一个稳定点:此时只追加了 goal,通常还不到阈值,但若上一轮 run
        # 已让历史很长,这里先压一次,避免第一个任务就带着超长上下文起步。
        self._maybe_compact()

        # ── 2. 执行 + 重规划循环 ──────────────────────────────────────────────
        rounds_left = self._max_steps
        last_text = ""
        try:
            while rounds_left > 0:
                cancel.raise_if_cancelled()
                task = self.store.claim_next()
                if task is None:
                    break  # 没有可跑的任务:要么收工,要么卡死(循环外判定)

                self._emit(RunEvent(kind=events.TASK_STARTED, task_id=task.id,
                                    task_content=task.content))

                # 每个任务只获得独立的模型往返上限；全局预算由 rounds_left 控制。
                # 不能把全部剩余额度一次性交给首个任务，否则多任务计划会饿死。
                budget = max(1, min(rounds_left, self._max_task_steps))
                outcome = self._executor.run_task(
                    task, self.messages,
                    system=self._system, max_steps=budget, cancel=cancel,
                    usage_acc=self.last_run_usage, on_actual_model=_record_model,
                )
                rounds_left -= max(1, outcome.model_rounds)
                self.last_run_tool_calls += outcome.tool_calls_made

                patch = self._replanner.apply(task, outcome)
                self._emit_task_update(task.id, patch.new_status, patch.note)
                if outcome.text:
                    last_text = outcome.text

                # 任务之间是干净的稳定点:此时 tool_use/tool_result 都已闭合,
                # 安全压缩旧历史(若预算触发)。
                self._maybe_compact()

        except Cancelled as exc:
            self.last_run_status = "cancelled"
            reason = str(exc).strip()
            text = reason or "已取消"
            self._emit(RunEvent(kind=events.RUN_FAILED, text=text,
                                payload={"tasks": self.store.snapshot()}))
            return text if reason else "已取消。"

        # ── 3. 收尾 ──────────────────────────────────────────────────────────
        snapshot = self.store.snapshot()
        if self.store.is_stalled():
            self.last_run_status = "blocked"
            self._emit(RunEvent(kind=events.RUN_FAILED, text="部分任务被阻塞或失败,无法继续",
                                payload={"tasks": snapshot}))
            return last_text or "部分任务未能完成(被阻塞或失败)。"

        if rounds_left <= 0 and self.store.has_open():
            self.last_run_status = "failed"
            self._emit(RunEvent(kind=events.RUN_FAILED,
                                text="达到最大模型往返次数仍未完成全部任务",
                                payload={"tasks": snapshot}))
            return last_text or "达到最大模型往返次数,任务未全部完成。"

        self.last_run_status = "completed"
        self._emit(RunEvent(kind=events.RUN_COMPLETED, text=last_text,
                            payload={"tasks": snapshot}))
        return last_text or "已完成。"

    def all_completed(self) -> bool:
        """是否所有任务都成功完成(无 failed/blocked)。供测试/UI 判定。"""
        snap = self.store.snapshot()
        return bool(snap) and all(t["status"] == COMPLETED for t in snap)
