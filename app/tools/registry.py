"""工具注册表 —— 管理 Agent 可以调用的所有能力。

使用场景:
  程序启动时把所有工具注册进来,Agent 循环通过 registry.schemas() 把工具描述
  发给模型,模型决定调哪个,Agent 再通过 registry.execute() 实际执行。

所有内置工具、MCP 工具和后续电脑控制动作都使用 ToolDescriptor。除了模型可见
schema,Descriptor 还携带 risk / target / scope / origin 等安全元数据,供审批和
审计使用。旧代码仍可导入 Tool,它是 ToolDescriptor 的兼容别名。
"""
from __future__ import annotations

import json
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from app.util.redact import redact


ApprovalResolver = Callable[[dict[str, Any]], Optional[str]]
AuditRedactor = Callable[[dict[str, Any], str], tuple[str, str]]
ToolAuditSink = Callable[["ToolAuditEvent"], None]

TOOL_RISKS = frozenset({
    "read",
    "observe",
    "network",
    "write",
    "browser_control",
    "desktop_control",
    "remote_execute",
    "execute",
    "destructive",
})

# 能力标签只用于决定模型看到哪些工具；ToolRegistry.execute() 始终使用完整注册表。
TOOL_CAPABILITIES = frozenset({
    "inspect",
    "filesystem_read",
    "filesystem_write",
    "code_search",
    "execute",
    "verify",
    "web_search",
    "web_fetch",
    "network",
    "custom",
})

# 未显式覆盖 requires_approval 时按风险决定。read 是本地无副作用读取;
# observe/network 需要按目标确认;其余会改变环境或执行代码,默认逐次审批。
_APPROVAL_REQUIRED_RISKS = TOOL_RISKS - {"read"}


class ToolExecutionError(Exception):
    """executor 可抛出的预期工具错误,消息会原样回传给模型。"""


def _clip(text: str, limit: int = 500) -> str:
    return text[:limit] + "…" if len(text) > limit else text


def _json_summary(value: Any) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, default=repr)
    except (TypeError, ValueError):
        text = repr(value)
    return redact(_clip(text))

# 审批结果不能放进模型可控的 args。Runtime 在 ToolRegistry.execute() 调用时
# 传入已批准 action，Registry 用 ContextVar 只在当前 executor 调用期间授予。
_approved_action: ContextVar[Optional[str]] = ContextVar(
    "approved_tool_action",
    default=None,
)


def is_approval_granted(action: str) -> bool:
    """当前工具执行上下文是否获得了指定审批动作。"""
    return _approved_action.get() == action


@dataclass
class ToolAuditEvent:
    """一次工具执行或拒绝的结构化审计事件。"""

    tool_name: str
    risk: str
    target_type: str
    scope: str
    origin: str
    host: Optional[str]
    requires_observation: bool
    approval_action: Optional[str]
    outcome: str
    args_summary: str
    result_summary: str
    is_error: bool
    elapsed_seconds: float


@dataclass
class ToolDescriptor:
    """一个可被模型调用、可审批、可审计的能力描述。

    requires_approval 是兼容覆盖项。设为 None 时根据 risk 使用默认策略;
    旧工具可继续显式传 True/False。approval_resolver 可按参数提高审批要求,
    例如 workspace 外路径返回独立且不可持久化的 action。
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    executor: Callable[[dict[str, Any]], str]
    # 保持旧 Tool 构造器的第 5/6 个位置参数顺序,兼容外部扩展。
    requires_approval: Optional[bool] = None
    approval_resolver: Optional[ApprovalResolver] = None
    risk: str = "read"
    target_type: str = "runtime"
    scope: str = "session"
    origin: str = "builtin"
    host: Optional[str] = None
    requires_observation: bool = False
    audit_redactor: Optional[AuditRedactor] = None
    # 工具能力标签：只影响模型可见集合，不影响 registry 的实际执行查找。
    capabilities: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.risk not in TOOL_RISKS:
            allowed = ", ".join(sorted(TOOL_RISKS))
            raise ValueError(
                f"unknown tool risk '{self.risk}' for {self.name}; expected one of: {allowed}"
            )
        capabilities = set(self.capabilities)
        if not capabilities:
            if self.name in {"read_file", "list_dir"}:
                capabilities.update({"inspect", "filesystem_read"})
            elif self.name == "code_search":
                capabilities.update({"inspect", "code_search", "filesystem_read"})
            elif self.name in {"write_file", "edit_file"}:
                capabilities.update({"filesystem_write"})
            elif self.name == "shell":
                capabilities.update({"execute", "verify"})
            elif self.name == "web_search":
                capabilities.update({"web_search", "network"})
            elif self.name == "web_fetch":
                capabilities.update({"web_fetch", "network"})
            elif self.risk == "read":
                capabilities.add("inspect")
            else:
                capabilities.add("custom")
        unknown = capabilities - TOOL_CAPABILITIES
        if unknown:
            allowed = ", ".join(sorted(TOOL_CAPABILITIES))
            raise ValueError(
                f"unknown tool capability '{sorted(unknown)[0]}' for {self.name}; "
                f"expected one of: {allowed}"
            )
        self.capabilities = frozenset(capabilities)

    def approval_action(self, args: dict[str, Any]) -> Optional[str]:
        """返回本次调用需要批准的动作名,无需审批则返回 None。"""
        if self.approval_resolver is not None:
            dynamic = self.approval_resolver(args or {})
            if dynamic:
                return dynamic
        needs_approval = (
            self.requires_approval
            if self.requires_approval is not None
            else self.risk in _APPROVAL_REQUIRED_RISKS
        )
        return self.name if needs_approval else None

    def audit_summary(
        self,
        args: dict[str, Any],
        result: str,
    ) -> tuple[str, str]:
        """生成脱敏、有界的参数和结果摘要。"""
        if self.audit_redactor is not None:
            try:
                args_summary, result_summary = self.audit_redactor(args or {}, result)
                return redact(_clip(str(args_summary))), redact(_clip(str(result_summary)))
            except Exception:
                # 审计回调不能影响工具主流程,失败时退回通用脱敏摘要。
                pass
        return _json_summary(args or {}), redact(_clip(str(result)))

    def to_schema(self) -> dict[str, Any]:
        """生成发给模型的工具描述字典(Anthropic 原生格式)。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


Tool = ToolDescriptor


class ToolRegistry:
    """工具注册表,存储所有已注册的工具并提供统一的执行入口。

    使用场景:
      1. 启动时调用 register() 把工具加进来
      2. 把 schemas() 的结果发给模型,让模型知道有哪些工具可用
      3. 模型返回工具调用请求后,调用 execute() 实际运行
    """

    def __init__(self, audit_sink: Optional[ToolAuditSink] = None) -> None:
        # 用字典存储,key 是工具名,方便按名称 O(1) 查找
        self._tools: dict[str, ToolDescriptor] = {}
        self._audit_sink = audit_sink

    def register(self, tool: ToolDescriptor) -> None:
        """注册一个工具。同名工具会覆盖旧的。"""
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDescriptor | None:
        """按名称查找工具。未注册时返回 None。"""
        return self._tools.get(name)

    def all(self) -> list[ToolDescriptor]:
        """返回所有已注册的工具列表。"""
        return list(self._tools.values())

    def schemas(
        self,
        *,
        capabilities: Iterable[str] | None = None,
        include: Iterable[str] | None = None,
        exclude: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """返回当前上下文需要暴露给模型的工具 Schema。

        默认保持旧行为返回全部工具；传入 capabilities/include/exclude 后只筛选
        模型可见集合，execute() 仍可查找完整注册表，避免动态暴露破坏历史调用。
        """
        selected = list(self._tools.values())
        if capabilities is not None:
            wanted = set(capabilities)
            selected = [tool for tool in selected if tool.capabilities & wanted]
        if include is not None:
            names = set(include)
            selected = [tool for tool in selected if tool.name in names]
        if exclude is not None:
            names = set(exclude)
            selected = [tool for tool in selected if tool.name not in names]
        return [tool.to_schema() for tool in selected]

    def schemas_for_task(
        self,
        task: str,
        *,
        mode: str = "direct",
        include: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """按任务文本选择最小工具集合；未识别时保守返回核心只读工具。"""
        text = (task or "").lower()
        selected = {"read_file", "code_search", "list_dir"}
        # 第三方/MCP 工具没有统一名称，保留其显式注册能力；内置高成本工具仍按需加入。
        selected.update(
            tool.name for tool in self._tools.values()
            if tool.origin != "builtin"
        )
        if any(word in text for word in (
            "写", "改", "编辑", "删除", "创建", "修复", "实现", "修改",
            "write", "edit", "delete", "create", "fix", "implement", "modify",
        )):
            selected.update({"write_file", "edit_file"})
        if any(word in text for word in (
            "测试", "运行", "构建", "lint", "test", "run", "build", "pytest",
        )) or mode == "loop":
            selected.add("shell")
        if any(word in text for word in (
            "最新", "联网", "网页", "新闻", "文档", "搜索", "latest", "web", "news",
        )):
            selected.update({"web_search", "web_fetch"})
        if include is not None:
            selected.update(include)
        return self.schemas(include=selected)

    def schemas_for_stage(
        self,
        stage: str,
        *,
        task: str = "",
        include: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        """按执行阶段选择工具；阶段规则优先于模糊任务关键词。"""
        normalized = (stage or "direct").lower()
        stage_tools = {
            "inspect": {"read_file", "list_dir", "code_search"},
            "analysis": {"read_file", "list_dir", "code_search"},
            "plan": set(),
            "direct": None,
            "task": None,
            "loop": None,
            "verify": {"shell", "read_file", "code_search"},
        }
        selected = stage_tools.get(normalized)
        if selected is None:
            return self.schemas_for_task(task, mode=normalized, include=include)
        if include is not None:
            selected = set(selected) | set(include)
        return self.schemas(include=selected)

    def schema_names(self, *, capabilities: Iterable[str] | None = None) -> tuple[str, ...]:
        """返回模型当前可见工具名称，便于事件和调试记录。"""
        selected = self.schemas(capabilities=capabilities)
        return tuple(str(item["name"]) for item in selected)

    def record_denied(
        self,
        name: str,
        args: dict[str, Any],
        *,
        approval_action: Optional[str],
    ) -> None:
        """记录上层 ApprovalPolicy 拒绝的调用。"""
        tool = self._tools.get(name)
        if tool is None:
            return
        self._emit_audit(
            tool,
            args,
            "User denied execution",
            approval_action=approval_action,
            outcome="denied",
            is_error=False,
            elapsed_seconds=0.0,
        )

    def _emit_audit(
        self,
        tool: ToolDescriptor,
        args: dict[str, Any],
        result: str,
        *,
        approval_action: Optional[str],
        outcome: str,
        is_error: bool,
        elapsed_seconds: float,
    ) -> None:
        if self._audit_sink is None:
            return
        args_summary, result_summary = tool.audit_summary(args, result)
        event = ToolAuditEvent(
            tool_name=tool.name,
            risk=tool.risk,
            target_type=tool.target_type,
            scope=tool.scope,
            origin=tool.origin,
            host=tool.host,
            requires_observation=tool.requires_observation,
            approval_action=approval_action,
            outcome=outcome,
            args_summary=args_summary,
            result_summary=result_summary,
            is_error=is_error,
            elapsed_seconds=elapsed_seconds,
        )
        try:
            self._audit_sink(event)
        except Exception:
            # 审计存储故障不能把已经完成的用户操作变成工具失败。
            pass

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        *,
        approved_action: Optional[str] = None,
    ) -> tuple[str, bool]:
        """执行指定工具,返回 (结果文本, 是否出错)。

        出错时不抛异常,而是把错误信息作为结果文本返回给模型,
        让模型看到错误后自行决定下一步(重试、换方案或告知用户)。

        approved_action 只能由 Runtime/Executor 在 ApprovalPolicy 放行后传入。
        Registry 会再次核对工具本次实际需要的动作，阻止绕过上层审批直接执行。
        """
        started = time.monotonic()
        tool = self._tools.get(name)
        if tool is None:
            return f"unknown tool: {name}", True
        required_action = tool.approval_action(args or {})
        if required_action is not None and approved_action != required_action:
            result = f"approval required: {required_action}"
            self._emit_audit(
                tool,
                args or {},
                result,
                approval_action=required_action,
                outcome="approval_required",
                is_error=True,
                elapsed_seconds=time.monotonic() - started,
            )
            return result, True

        token = _approved_action.set(approved_action)
        try:
            result = tool.executor(args or {})
            is_error = False
        except ToolExecutionError as exc:
            result = str(exc)
            is_error = True
        except Exception as exc:
            result = f"{type(exc).__name__}: {exc}"
            is_error = True
        finally:
            _approved_action.reset(token)
        self._emit_audit(
            tool,
            args or {},
            result,
            approval_action=required_action,
            outcome="error" if is_error else "completed",
            is_error=is_error,
            elapsed_seconds=time.monotonic() - started,
        )
        return result, is_error
