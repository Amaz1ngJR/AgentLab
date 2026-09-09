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
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.tools.registry import ToolDescriptor


class ApprovalResult:
    """审批结果。

    feedback 不代表普通拒绝，而是用户要求暂停当前 turn、回到主聊天输入框后再
    提交下一条指令；Runtime 不应把反馈自动喂给模型继续 thinking。
    """
    def __init__(self, approved: bool, feedback: str | None = None, cancelled: bool = False):
        self.approved = approved
        self.feedback = feedback  # 用户的修改建议
        self.cancelled = cancelled  # 用户是否按了 ESC/Ctrl-C 明确取消


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


def request_tool_approval(
    policy: ApprovalPolicy,
    tool: "ToolDescriptor",
    action: str,
    tool_input: dict[str, Any],
) -> ApprovalResult:
    """用结构化工具元数据请求审批,并兼容旧 ApprovalPolicy。

    新策略可实现 request_tool();现有测试策略和第三方策略只实现 request() 也能
    继续工作。模型参数保持原样,风险元数据不会混进 executor 的 args。

    返回 ApprovalResult，包含审批结果和可选的用户反馈。
    """
    extended = getattr(policy, "request_tool", None)
    if callable(extended):
        result = extended(tool, action, tool_input)
        if isinstance(result, ApprovalResult):
            return result
        return ApprovalResult(approved=bool(result))

    # 兼容旧接口
    approved = bool(policy.request(action, tool_input))
    return ApprovalResult(approved=approved)


class AutoApprove:
    """无条件放行。

    使用场景:
      - 命令行加 -y 参数时使用(python -m app -y)
      - 自动化脚本、CI 流水线中不需要人工干预时使用
    """

    def request(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        return True

    def request_tool(
        self,
        tool: "ToolDescriptor",
        action: str,
        tool_input: dict[str, Any],
    ) -> ApprovalResult:
        return ApprovalResult(approved=True)


class DenyAll:
    """无条件拒绝。

    使用场景:
      - 单元测试中验证"工具被拒绝后模型如何响应"
      - 只读模式:只允许 read_file / list_dir,禁止任何写操作
    """

    def request(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        return False

    def request_tool(
        self,
        tool: "ToolDescriptor",
        action: str,
        tool_input: dict[str, Any],
    ) -> ApprovalResult:
        return ApprovalResult(approved=False)


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
    _NON_PERSISTENT_ACTIONS = {
        "shell", "terminal_open", "terminal_send", "clear_session_images",
    }
    _NON_PERSISTENT_RISKS = {
        "browser_control",
        "desktop_control",
        "remote_execute",
        "execute",
        "destructive",
    }

    def __init__(self) -> None:
        # 本会话的白名单:选过"总是允许"的工具名存在这里,下次直接放行
        self._always: set[str] = set()

    def _format_tool_args(self, tool_name: str, tool_input: dict[str, Any]) -> list[str]:
        """格式化工具参数，参考 Claude Code 简洁风格。"""
        lines = []

        # shell/bash: 显示 command + 其他参数
        if tool_name in ("shell", "bash") and "command" in tool_input:
            cmd = tool_input["command"]
            lines.append(f"● {tool_name}")
            lines.append(f"  {cmd}")

            # 显示其他参数（cwd、timeout 等）
            other_params = []
            if "cwd" in tool_input and tool_input["cwd"]:
                other_params.append(f"cwd: {tool_input['cwd']}")
            if "timeout" in tool_input:
                other_params.append(f"timeout: {tool_input['timeout']}s")
            for key in sorted(tool_input.keys()):
                if key not in ("command", "cwd", "timeout"):
                    other_params.append(f"{key}: {tool_input[key]}")

            if other_params:
                lines.append(f"  ({', '.join(other_params)})")
            return lines

        # read/write/edit: 显示 file_path + 其他参数
        if tool_name in ("read", "write", "edit"):
            file_path = tool_input.get("file_path", "")
            lines.append(f"● {tool_name}({file_path})")

            # 显示其他参数
            other_params = []
            if tool_name in ("write", "edit") and "content" in tool_input:
                content_len = len(str(tool_input["content"]))
                if content_len > 1000:
                    other_params.append(f"{content_len // 1000}KB content")
                else:
                    other_params.append(f"{content_len}B content")

            for key in sorted(tool_input.keys()):
                if key not in ("file_path", "content"):
                    other_params.append(f"{key}: {tool_input[key]}")

            if other_params:
                lines.append(f"  ({', '.join(other_params)})")
            return lines

        # 默认：简洁显示工具名 + 参数摘要
        try:
            args_str = json.dumps(tool_input, ensure_ascii=False)
        except (TypeError, ValueError):
            args_str = repr(tool_input)

        # 截断过长参数
        if len(args_str) > 100:
            args_str = args_str[:100] + "..."

        lines.append(f"● {tool_name}({args_str})")
        return lines

    def _build_approval_choices(
        self,
        tool_name: str,
        action: str,
        tool: Optional["ToolDescriptor"],
        can_persist: bool,
    ) -> tuple[list[tuple[str, str]], str]:
        """根据工具类型和风险动态构造选项。

        返回 (choices, persist_label) 元组。
        """
        choices = [("接受", "yes")]

        # 高危操作：不能持久化，但保留"修改建议"
        is_high_risk = tool and tool.risk in self._NON_PERSISTENT_RISKS
        if is_high_risk:
            choices.append(("修改建议", "modify"))
            choices.append(("拒绝", "no"))
            return choices, ""

        # shell/bash: 提供"自动审批类似命令"
        if can_persist and tool_name in ("shell", "bash", "terminal_send"):
            choices.append(("本会话自动审批类似命令", "auto_approve"))

        # read/write: 提供"总是允许此工具"
        elif can_persist and tool_name in ("read", "write", "edit", "list_files"):
            choices.append((f"本会话总是允许 {tool_name}", "always"))

        # 其他可持久化工具
        elif can_persist:
            choices.append((f"本会话总是允许 {tool_name}", "always"))

        # 修改建议和拒绝
        choices.append(("修改建议", "modify"))
        choices.append(("拒绝", "no"))

        return choices, ""

    def request(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        result = self._request(tool_name, tool_input)
        return result.approved

    def request_tool(
        self,
        tool: "ToolDescriptor",
        action: str,
        tool_input: dict[str, Any],
    ) -> ApprovalResult:
        """带 risk/target/origin 元数据的工具审批入口。"""
        return self._request(action, tool_input, action=action, tool=tool)

    def _request(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        *,
        action: str = "",
        tool: "ToolDescriptor | None" = None,
    ) -> ApprovalResult:
        risk = getattr(tool, "risk", None)
        can_persist = (
            tool_name not in self._NON_PERSISTENT_ACTIONS
            and not tool_name.endswith("_outside_workspace")
            and risk not in self._NON_PERSISTENT_RISKS
        )
        # 已在白名单中,直接放行,不再询问
        if can_persist and tool_name in self._always:
            return ApprovalResult(approved=True)

        # 延迟导入避免 prompt_toolkit 在 AutoApprove 场景下也被加载
        from app.util.menu import select_menu

        # ── 构造 header：简洁风格，参考 Claude Code ──
        header_lines = []

        # 格式化参数：关键参数单独成行，便于阅读
        formatted_args = self._format_tool_args(tool_name, tool_input)
        header_lines.extend(formatted_args)

        # ── 动态构造选项：根据工具类型和风险等级调整 ──
        choices, persist_label = self._build_approval_choices(
            tool_name, action, tool, can_persist
        )

        result = select_menu(
            choices=choices,
            header_lines=header_lines,
            title="是否允许执行?",
        )

        if result == "yes":
            return ApprovalResult(approved=True)
        if result == "always" and can_persist:
            self._always.add(tool_name)  # 加入白名单
            return ApprovalResult(approved=True)
        if result == "auto_approve" and can_persist:
            # 自动审批同类操作（如 shell 命令、read 文件）
            self._always.add(f"{tool_name}:{action}" if action else tool_name)
            return ApprovalResult(approved=True)
        if result == "modify":
            # "修改建议"是暂停动作，不在审批弹窗里再开一个输入框。这样语义与用户
            # 预期一致：立即停止当前 turn，回到主聊天框，由用户输入完整下一步指令。
            return ApprovalResult(
                approved=False,
                feedback="用户选择修改建议；已暂停当前任务，等待下一条用户指令。",
                cancelled=True,
            )
        if result == "no":
            # 明确选择"拒绝"：Agent 自行调整
            return ApprovalResult(approved=False)
        # None(ESC/Ctrl-C 取消)：用户想中断当前操作
        return ApprovalResult(approved=False, cancelled=True)
