"""人工审批策略 —— 控制模型是否被允许执行危险工具。

使用场景:
  Agent 循环在执行 requires_approval=True 的工具(如 write_file)之前,
  先调用 ApprovalPolicy.request() 询问是否放行。
  根据场景选择不同策略:
    - 交互式终端使用 InteractivePolicy,弹出方向键菜单让用户选
    - 自动化脚本 / 测试使用 AutoApprove,跳过询问直接放行
    - 单元测试验证"拒绝"逻辑时使用 DenyAll
"""
from __future__ import annotations

import json
from typing import Any, Protocol


class ApprovalPolicy(Protocol):
    """审批策略接口。

    所有策略类只需实现 request() 方法即可,不需要继承任何基类。
    Python 的 Protocol 机制会自动检查是否符合接口(鸭子类型)。
    """

    def request(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        """询问是否允许执行某个工具。

        tool_name  - 工具名称,例如 "write_file"
        tool_input - 工具参数,例如 {"path": "/tmp/x.txt", "content": "hello"}
        返回 True 表示允许执行,False 表示拒绝。
        """
        ...


class AutoApprove:
    """无条件放行。

    使用场景:
      - 命令行加 -y 参数时使用(python -m app -y)
      - 自动化脚本、CI 流水线中不需要人工干预时使用
    """

    def request(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        return True


class DenyAll:
    """无条件拒绝。

    使用场景:
      - 单元测试中验证"工具被拒绝后模型如何响应"
      - 只读模式:只允许 read_file / list_dir,禁止任何写操作
    """

    def request(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        return False


class InteractivePolicy:
    """命令行交互式审批 —— 弹出方向键菜单让用户选择。

    使用场景:默认的交互式终端模式,用户可以逐次决定是否允许。

    UI 风格(类似 Claude Code):
        工具: write_file
        参数: {"path": "/tmp/x.txt", ...}

        是否允许执行?
        ❯ 1. 允许这次
          2. 本会话总是允许 write_file
          3. 拒绝

        ↑↓ 移动 · Enter 确认 · 1-9 快捷键 · Esc 取消

    三个选项:
      允许这次   - 允许这一次,下次同一工具还会再问
      总是允许   - 本会话内该工具名加入白名单,后续不再询问
      拒绝      - 模型会收到"用户拒绝"消息并自行决定下一步

    Ctrl-C / Esc 视为拒绝,安全退出。
    """

    # 工具参数预览的最大字符数,过长会截断
    _PREVIEW_MAX = 240
    # workspace 越界和任意命令执行必须逐次确认，不能被会话白名单吞掉。
    _NON_PERSISTENT_ACTIONS = {"shell", "terminal_open", "terminal_send"}

    def __init__(self) -> None:
        # 本会话的白名单:选过"总是允许"的工具名存在这里,下次直接放行
        self._always: set[str] = set()

    def request(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        can_persist = (
            tool_name not in self._NON_PERSISTENT_ACTIONS
            and not tool_name.endswith("_outside_workspace")
        )
        # 已在白名单中,直接放行,不再询问
        if can_persist and tool_name in self._always:
            return True

        # 延迟导入避免 prompt_toolkit 在 AutoApprove 场景下也被加载
        from app.util.menu import select_menu

        # ── 构造 header:展示工具调用上下文,让用户清楚自己在批准什么 ──
        try:
            args_str = json.dumps(tool_input, ensure_ascii=False)
        except (TypeError, ValueError):
            args_str = repr(tool_input)
        if len(args_str) > self._PREVIEW_MAX:
            args_str = args_str[: self._PREVIEW_MAX] + "…"

        header_lines = [
            f"工具: {tool_name}",
            f"参数: {args_str}",
        ]

        choices = [("允许这次", "yes")]
        if can_persist:
            choices.append((f"本会话总是允许 {tool_name}", "always"))
        choices.append(("拒绝", "no"))

        result = select_menu(
            choices=choices,
            header_lines=header_lines,
            title="是否允许执行?",
        )

        if result == "yes":
            return True
        if result == "always" and can_persist:
            self._always.add(tool_name)  # 加入白名单
            return True
        # "no" 或 None(取消) 都视为拒绝
        return False
