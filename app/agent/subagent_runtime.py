"""创建真实子 Agent 运行时，并把权限限制在父 Agent 的工具集合内。"""
from __future__ import annotations

import threading
from typing import Any, Callable

from app.agent.approval import ApprovalPolicy, AutoApprove
from app.agent.cancel import CancelToken
from app.agent.events import RunEvent
from app.agent.execution_plan import ROLE_TOOLS
from app.agent.subagents import Delegate, SubagentResult, SubagentSpec
from app.agent.tasks import TaskStore
from app.config.loader import use_workspace_root
from app.models.router import ModelRouter
from app.tools.registry import Tool, ToolRegistry

class RestrictedToolRegistry(ToolRegistry):
    """保留完整注册表查找，但只允许指定工具执行。"""

    def __init__(self, source: ToolRegistry, allowed: set[str]):
        super().__init__()
        self._source = source
        self._allowed = frozenset(allowed)

    def get(self, name: str) -> Tool | None:
        if name not in self._allowed:
            return None
        return self._source.get(name)

    def all(self) -> list[Tool]:
        return [tool for tool in self._source.all() if tool.name in self._allowed]

    def schemas(self, **kwargs) -> list[dict[str, Any]]:
        return [tool.to_schema() for tool in self.all()]

    def schemas_for_task(self, task: str, *, mode: str = "task", **kwargs) -> list[dict[str, Any]]:
        return self.schemas()

    def execute(self, name: str, args: dict[str, Any], *, approved_action: str | None = None):
        if name not in self._allowed:
            return f"tool not allowed for this subagent: {name}", True
        return self._source.execute(name, args, approved_action=approved_action)


class SubagentRuntimeFactory:
    """用独立 messages/TaskStore 构建子 Agent delegate。"""

    def __init__(
        self,
        *,
        llm: ModelRouter,
        tools: ToolRegistry,
        approval: ApprovalPolicy | None = None,
        parent_system: str = "",
        on_event: Callable[[RunEvent], None] | None = None,
        max_steps: int = 16,
        max_tool_calls: int = 60,
    ):
        self.llm = llm
        self.tools = tools
        self.approval = approval or AutoApprove()
        self.parent_system = parent_system
        self.on_event = on_event
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self._active_lock = threading.Lock()
        self._active_tool_calls = 0

    def delegate(self, spec: SubagentSpec, workspace: Path, cancel: CancelToken) -> SubagentResult:
        from app.agent.orchestrator import Orchestrator
        with self._active_lock:
            self._active_tool_calls = 0
        allowed = set(spec.tools) if spec.tools else set(ROLE_TOOLS.get(spec.role, ()))
        allowed &= set(ROLE_TOOLS.get(spec.role, ()))
        allowed &= {tool.name for tool in self.tools.all()}
        if spec.read_only:
            allowed -= {"write_file", "edit_file"}
        child_tools = RestrictedToolRegistry(self.tools, allowed)
        child_system = (
            f"{self.parent_system}\n\n"
            "【子 Agent 约束】\n"
            "你是受主 Agent 委托的子 Agent，只完成当前指令。\n"
            "不能创建子 Agent，不能修改审批策略，不能自动合并或推送。\n"
            f"当前隔离工作区: {workspace}\n"
        )
        child = Orchestrator(
            llm=self.llm,
            tools=child_tools,
            approval=self.approval,
            system=child_system,
            max_steps=min(spec.max_steps, self.max_steps),
            max_task_steps=min(spec.max_steps, self.max_steps),
            task_store=TaskStore(),
            on_event=self.on_event,
            messages=[],
        )
        with use_workspace_root(workspace):
            cancel.raise_if_cancelled()
            output = child.run(spec.instruction, cancel=cancel)
        status = "succeeded" if child.last_run_status == "completed" else "failed"
        return SubagentResult(
            subagent_id=spec.subagent_id,
            role=spec.role,
            status=status,
            output=output[:spec.output_limit],
            error="" if status == "succeeded" else (child.last_run_error or output[:1000]),
        )

    def as_delegate(self) -> Delegate:
        return self.delegate
