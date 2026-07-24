"""工具注册表 —— 管理 Agent 可以调用的所有工具。

使用场景:
  程序启动时把所有工具注册进来,Agent 循环通过 registry.schemas() 把工具描述
  发给模型,模型决定调哪个,Agent 再通过 registry.execute() 实际执行。

工具的关键属性:
  input_schema      - 告诉模型这个工具接受哪些参数(JSON Schema 格式)
  requires_approval - True 表示执行前必须人工确认(写文件、执行命令等危险操作)
  approval_resolver - 根据参数返回动态审批动作;例如只在路径越出 workspace 时审批
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Optional


ApprovalResolver = Callable[[dict[str, Any]], Optional[str]]

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
class Tool:
    """一个可被模型调用的工具。

    name              - 工具名称,模型用这个名字请求调用,例如 "read_file"
    description       - 工具功能描述,模型根据这段文字决定要不要用它
    input_schema      - 参数定义(JSON Schema),告诉模型该传哪些字段
    executor          - 实际执行函数,接收参数字典,返回结果字符串
    requires_approval - 是否需要人工确认才能执行。
                        只读操作(read_file / list_dir)设为 False,
                        写操作(write_file)或危险操作(shell 命令)设为 True。
    approval_resolver - 可选动态审批判定。返回审批动作名表示本次需要审批,
                        返回 None 表示沿用 requires_approval。
    """
    name: str
    description: str
    input_schema: dict[str, Any]
    executor: Callable[[dict[str, Any]], str]
    requires_approval: bool = False
    approval_resolver: Optional[ApprovalResolver] = None

    def approval_action(self, args: dict[str, Any]) -> Optional[str]:
        """返回本次调用需要批准的动作名,无需审批则返回 None。"""
        if self.approval_resolver is not None:
            dynamic = self.approval_resolver(args or {})
            if dynamic:
                return dynamic
        return self.name if self.requires_approval else None

    def to_schema(self) -> dict[str, Any]:
        """生成发给模型的工具描述字典(Anthropic 原生格式)。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    """工具注册表,存储所有已注册的工具并提供统一的执行入口。

    使用场景:
      1. 启动时调用 register() 把工具加进来
      2. 把 schemas() 的结果发给模型,让模型知道有哪些工具可用
      3. 模型返回工具调用请求后,调用 execute() 实际运行
    """

    def __init__(self) -> None:
        # 用字典存储,key 是工具名,方便按名称 O(1) 查找
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具。同名工具会覆盖旧的。"""
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        """按名称查找工具。未注册时返回 None。"""
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        """返回所有已注册的工具列表。"""
        return list(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        """返回所有工具的描述字典列表,直接传给模型的 tools 参数。"""
        return [t.to_schema() for t in self._tools.values()]

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
        tool = self._tools.get(name)
        if tool is None:
            return f"unknown tool: {name}", True
        required_action = tool.approval_action(args or {})
        if required_action is not None and approved_action != required_action:
            return f"approval required: {required_action}", True

        token = _approved_action.set(approved_action)
        try:
            return tool.executor(args or {}), False
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}", True
        finally:
            _approved_action.reset(token)
