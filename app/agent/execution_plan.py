"""Planner 生成的声明式执行计划及其安全校验。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.agent.subagents import SubagentSpec
from app.agent.tasks import PENDING, Task

ROLES = frozenset({"research", "reviewer", "verifier", "executor"})
EXECUTORS = frozenset({"main_agent", "subagent"})
READ_ONLY_TOOLS = frozenset({"read_file", "list_dir", "code_search"})
WRITE_TOOLS = frozenset({"write_file", "edit_file"})
FORBIDDEN_SUBAGENT_TOOLS = frozenset({"delegate_subagent", "spawn_subagent", "merge_worktree", "force_push"})
ROLE_TOOLS = {
    "research": READ_ONLY_TOOLS,
    "reviewer": READ_ONLY_TOOLS | {"shell"},
    "verifier": READ_ONLY_TOOLS | {"shell"},
    "executor": READ_ONLY_TOOLS | WRITE_TOOLS | {"shell"},
}


@dataclass(frozen=True)
class ExecutionPolicy:
    mode: str = "hybrid"
    max_parallel: int = 2
    merge_policy: str = "manual"
    failure_policy: str = "block_dependents"
    allow_nested_subagents: bool = False


@dataclass
class PlannedTask:
    task: Task
    executor: str = "main_agent"
    subagent: SubagentSpec | None = None


@dataclass
class ExecutionPlan:
    version: int
    goal: str
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    tasks: list[PlannedTask] = field(default_factory=list)

    @property
    def subagent_specs(self) -> list[SubagentSpec]:
        return [item.subagent for item in self.tasks if item.subagent is not None]


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_spec(item: dict[str, Any], task_id: str) -> SubagentSpec | None:
    raw = item.get("subagent")
    if not isinstance(raw, dict):
        return None
    role = _as_str(raw.get("role")) or "executor"
    sid = _as_str(raw.get("id")) or f"subagent-{task_id}"
    instruction = _as_str(raw.get("instruction")) or _as_str(item.get("content"))
    deps = raw.get("dependencies", item.get("dependencies", []))
    dependencies = tuple(str(dep).strip() for dep in deps if str(dep).strip()) if isinstance(deps, list) else ()
    tools = raw.get("tools", [])
    requested_tools = tuple(str(tool).strip() for tool in tools if str(tool).strip()) if isinstance(tools, list) else ()
    return SubagentSpec(
        subagent_id=sid,
        instruction=instruction,
        role=role,
        dependencies=dependencies,
        read_only=bool(raw.get("read_only", role != "executor")),
        worktree_id=_as_str(raw.get("worktree_id")) or None,
        tools=requested_tools,
        max_steps=max(1, _int(raw.get("max_steps"), 8)),
        max_tool_calls=max(1, _int(raw.get("max_tool_calls"), 30)),
        timeout_seconds=max(1, _int(raw.get("timeout_seconds"), 300)),
        file_scope=tuple(str(path).strip() for path in (raw.get("file_scope") or []) if str(path).strip()),
        retry_limit=max(0, _int(raw.get("retry_limit"), 0)),
        output_limit=max(1, _int(raw.get("output_limit"), 20_000)),
    )


def parse_execution_plan(obj: dict[str, Any], goal: str) -> ExecutionPlan:
    """解析新协议；旧版只含 tasks 时返回全 main_agent 计划。"""
    if not isinstance(obj, dict):
        raise ValueError("执行计划必须是 JSON 对象")
    version = _int(obj.get("version"), 0)
    raw_policy = obj.get("execution") or {}
    if not isinstance(raw_policy, dict):
        raw_policy = {}
    policy = ExecutionPolicy(
        mode=_as_str(raw_policy.get("mode")) or ("single_agent" if version == 0 else "hybrid"),
        max_parallel=max(1, _int(raw_policy.get("max_parallel"), 2)),
        merge_policy=_as_str(raw_policy.get("merge_policy")) or "manual",
        failure_policy=_as_str(raw_policy.get("failure_policy")) or "block_dependents",
        allow_nested_subagents=bool(raw_policy.get("allow_nested_subagents", False)),
    )
    raw_tasks = obj.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValueError("执行计划 tasks 必须是数组")
    planned: list[PlannedTask] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_tasks, 1):
        if not isinstance(raw, dict):
            continue
        task_id = _as_str(raw.get("id")) or f"t{index}"
        content = _as_str(raw.get("content")) or _as_str(raw.get("description"))
        if not content or task_id in seen:
            continue
        seen.add(task_id)
        deps_raw = raw.get("dependencies") or []
        deps = [str(dep).strip() for dep in deps_raw if str(dep).strip()] if isinstance(deps_raw, list) else []
        task = Task(id=task_id, content=content, status=PENDING, dependencies=deps)
        executor = _as_str(raw.get("executor")) or "main_agent"
        spec = _parse_spec(raw, task_id) if executor == "subagent" else None
        planned.append(PlannedTask(task=task, executor=executor, subagent=spec))
    return ExecutionPlan(version=version, goal=goal, policy=policy, tasks=planned)


def validate_execution_plan(
    plan: ExecutionPlan,
    *,
    allowed_tools: set[str] | None = None,
    max_subagents: int = 8,
    max_parallel: int = 4,
    max_steps: int = 32,
) -> list[str]:
    errors: list[str] = []
    if plan.version not in {0, 1}:
        errors.append(f"不支持的执行计划版本: {plan.version}")
    if plan.policy.mode not in {"single_agent", "subagents", "hybrid"}:
        errors.append(f"不支持的执行模式: {plan.policy.mode}")
    if plan.policy.max_parallel > max_parallel:
        errors.append(f"并发数超过上限: {plan.policy.max_parallel} > {max_parallel}")
    if plan.policy.merge_policy != "manual":
        errors.append("merge_policy 必须为 manual")
    ids = {item.task.id for item in plan.tasks}
    subagent_ids: set[str] = set()
    subagents = 0
    for item in plan.tasks:
        if item.executor not in EXECUTORS:
            errors.append(f"任务 {item.task.id} executor 非法: {item.executor}")
        if any(dep not in ids for dep in item.task.dependencies):
            errors.append(f"任务 {item.task.id} 存在不存在的依赖")
        if item.executor != "subagent":
            continue
        subagents += 1
        spec = item.subagent
        if spec is None:
            errors.append(f"任务 {item.task.id} 缺少 subagent 定义")
            continue
        if spec.subagent_id in subagent_ids:
            errors.append(f"重复的 subagent_id: {spec.subagent_id}")
        subagent_ids.add(spec.subagent_id)
        if spec.role not in ROLES:
            errors.append(f"子 Agent {spec.subagent_id} role 非法: {spec.role}")
        if not spec.instruction:
            errors.append(f"子 Agent {spec.subagent_id} instruction 不能为空")
        if spec.role != "executor" and not spec.read_only:
            errors.append(f"只读角色不能写入: {spec.subagent_id}")
        requested = set(spec.tools)
        if requested & FORBIDDEN_SUBAGENT_TOOLS:
            errors.append(f"子 Agent 请求了禁止工具: {spec.subagent_id}")
        role_allowed = ROLE_TOOLS.get(spec.role, frozenset())
        if requested - role_allowed:
            errors.append(f"子 Agent 工具超出角色权限: {spec.subagent_id}")
        if allowed_tools is not None and requested - allowed_tools:
            errors.append(f"子 Agent 工具不在父级允许集合: {spec.subagent_id}")
        if spec.max_steps > max_steps:
            errors.append(f"子 Agent 步数超过上限: {spec.subagent_id}")
        if not spec.read_only and not spec.worktree_id and plan.policy.mode == "single_agent":
            errors.append(f"写入子 Agent 不能运行在 single_agent 模式: {spec.subagent_id}")
    if subagents > max_subagents:
        errors.append(f"子 Agent 数量超过上限: {subagents} > {max_subagents}")
    # Kahn 拓扑排序：依赖环必须在计划阶段拒绝，而不是让运行时永久等待。
    graph = {item.task.id: set(item.task.dependencies) for item in plan.tasks}
    while graph:
        ready = {tid for tid, deps in graph.items() if not deps}
        if not ready:
            errors.append("任务依赖存在循环")
            break
        for tid in ready:
            graph.pop(tid)
        for deps in graph.values():
            deps.difference_update(ready)
    return errors
