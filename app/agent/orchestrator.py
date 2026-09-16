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
from app.agent.executor import Executor, TaskOutcome
from app.agent.planner import Planner
from app.agent.replanner import Replanner
from app.agent.tasks import BLOCKED, COMPLETED, FAILED, TaskStore
from app.agent.execution_plan import ExecutionPlan
from app.agent.subagent_runtime import SubagentRuntimeFactory
from app.agent.subagents import SubagentCoordinator, SubagentResult, SubagentSpec
from app.config.loader import workspace_root
from app.workspace.worktree import WorktreeManager
from app.tools.registry import ToolRegistry

ProgressFn = Callable[[str], ContextManager[Any]]


def _extract_skill_context(system_prompt: str) -> str:
    """从完整 system prompt 中提取 skill 相关片段，供 Planner 使用。

    只提取 skill/workflow 部分，过滤掉 MCP 服务器列表、记忆等其他内容，
    避免 Planner 接收到重复的基础指令和大量无关上下文。
    """
    if not system_prompt:
        return ""

    # 查找 skill 相关的标记性片段（根据 SkillCatalog.build_skill_context 的输出格式）
    lines = system_prompt.split("\n")
    skill_lines = []
    in_skill_section = False

    for line in lines:
        # 检测 skill 章节开始（典型标记：## Available skills / # User-invocable skills）
        if any(marker in line.lower() for marker in [
            "available skills", "user-invocable skills", "技能列表", "可用技能"
        ]):
            in_skill_section = True
            skill_lines.append(line)
            continue

        # 如果在 skill 章节内
        if in_skill_section:
            # 检测章节结束（遇到新的一级/二级标题，但不是 skill 相关的）
            if line.startswith("#") and not any(marker in line.lower() for marker in [
                "skill", "workflow", "技能", "工作流"
            ]):
                in_skill_section = False
                continue
            skill_lines.append(line)

    return "\n".join(skill_lines).strip()


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
        self.last_run_error: str = ""
        self.subagent_results: dict[str, SubagentResult] = {}
        self.subagent_merge_plan: list[dict[str, Any]] = []
        self._planned_subagents: dict[str, SubagentSpec] = {}

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

    def _tools_for_task(self, task: str, *, mode: str = "task") -> list[dict[str, Any]]:
        """按任务文本为 Planner/Executor 提供动态工具 Schema。"""
        selector = getattr(self._tools, "schemas_for_task", None)
        if callable(selector):
            return selector(task, mode=mode)
        return self._tools.schemas()

    def _forward(self, ev: RunEvent) -> None:
        """Executor 的事件直接转发给外部 on_event。"""
        self._emit(ev)

    def _emit_task_update(self, task_id: str, status: str, note: str = "") -> None:
        self._emit(RunEvent(
            kind=events.TASK_UPDATED, task_id=task_id, task_status=status,
            text=note, payload={"tasks": self.store.snapshot()},
        ))

    def _maybe_compact(self, tools: list[dict[str, Any]] | None = None) -> None:
        """在稳定点或模型调用前检查上下文预算并按需压缩。"""
        if self._ctx is None:
            return
        tools = tools if tools is not None else self._tools.schemas() or None
        try:
            with self._progress("compacting") as handle:
                self._ctx.compact_before_model_call(
                    self.messages,
                    system=self._system,
                    tools=tools,
                    on_progress=getattr(handle, "update", None),
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
        self.last_run_error = ""
        self.last_run_tool_calls = 0
        self.subagent_results = {}
        self.subagent_merge_plan = []
        self._planned_subagents = {}
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
            # Planner 本身不接收工具，但压缩要按随后执行阶段的工具集算预算，
            # 否则规划后第一次执行调用就可能直接撞上窗口。
            self._maybe_compact(self._tools_for_task(goal))
            with self._progress("planning") as handle:
                on_progress = getattr(handle, "update", None)
                # 提取 skill 相关内容传给 Planner，让它能按 skill 指示拆任务。
                # 只传 skill 片段，避免重复传递 MCP 服务器列表、记忆等完整 system。
                skill_context = _extract_skill_context(self._system)
                plan = self._planner.create_plan(
                    goal,
                    context=skill_context,
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
        if plan.execution_plan is not None:
            self._planned_subagents = {
                item.task.id: item.subagent
                for item in plan.execution_plan.tasks
                if item.executor == "subagent" and item.subagent is not None
            }
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
                spec = self._planned_subagents.get(task.id)
                if spec is not None:
                    outcome = self._run_subagent_task(spec, task, cancel)
                else:
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
                self._maybe_compact(self._tools_for_task(task.content))

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
            self.last_run_error = "部分任务被阻塞或失败,无法继续"
            self._emit(RunEvent(kind=events.RUN_FAILED, text="部分任务被阻塞或失败,无法继续",
                                payload={"tasks": snapshot}))
            return last_text or "部分任务未能完成(被阻塞或失败)。"

        if rounds_left <= 0 and self.store.has_open():
            self.last_run_status = "failed"
            self.last_run_error = "达到最大模型往返次数仍未完成全部任务"
            self._emit(RunEvent(kind=events.RUN_FAILED,
                                text="达到最大模型往返次数仍未完成全部任务",
                                payload={"tasks": snapshot}))
            return last_text or "达到最大模型往返次数,任务未全部完成。"

        self.last_run_status = "completed"
        self.last_run_error = ""
        self._emit(RunEvent(kind=events.RUN_COMPLETED, text=last_text,
                            payload={"tasks": snapshot}))
        return last_text or "已完成。"

    def _run_subagent_task(self, spec: SubagentSpec, task, cancel: CancelToken) -> TaskOutcome:
        """执行一个已通过 Planner 校验的子 Agent 任务。"""
        def _delegate(spec: SubagentSpec, workspace, child_cancel):
            return factory.delegate(spec, workspace, child_cancel)

        try:
            if not spec.read_only:
                manager = WorktreeManager(workspace_root())
                coordinator = SubagentCoordinator(
                    manager, _delegate, max_workers=1, on_event=self._emit,
                )
                results = coordinator.run([spec], cancel=cancel)
                self.subagent_results.update(results)
                self.subagent_merge_plan.extend(coordinator.merge_plan(results))
            else:
                result = factory.delegate(spec, workspace_root(), cancel)
                self.subagent_results[spec.subagent_id] = result
        except Cancelled:
            raise
        except (ValueError, RuntimeError) as exc:
            return TaskOutcome(FAILED, error=str(exc), text=str(exc))
        except Exception as exc:
            return TaskOutcome(FAILED, error=f"{type(exc).__name__}: {exc}", text=str(exc))

        result = self.subagent_results[spec.subagent_id]
        status = COMPLETED if result.status == "succeeded" else FAILED
        evidence = result.output or result.error
        return TaskOutcome(status, evidence=evidence, error=result.error, text=result.output)

    def all_completed(self) -> bool:
        """是否所有任务都成功完成(无 failed/blocked)。供测试/UI 判定。"""
        snap = self.store.snapshot()
        return bool(snap) and all(t["status"] == COMPLETED for t in snap)
