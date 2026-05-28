"""人工审批策略 —— 控制模型是否被允许执行危险工具。

使用场景:
  Agent 循环在执行 requires_approval=True 的工具(如 write_file)之前,
  先调用 ApprovalPolicy.request() 询问是否放行。
  根据场景选择不同策略:
    - 交互式终端使用 InteractivePolicy,每次弹出 y/a/n 提示
    - 自动化脚本 / 测试使用 AutoApprove,跳过询问直接放行
    - 单元测试验证"拒绝"逻辑时使用 DenyAll
"""
from __future__ import annotations

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
    """命令行交互式审批 —— 每次弹出提示让用户选择。

    使用场景:默认的交互式终端模式,用户可以逐次决定是否允许。

    提示格式:
        ? 允许执行 write_file?  [y]这次  [a]本会话总是  [n]拒绝 >

    三个选项:
      y (yes)   - 允许这一次,下次同一工具还会再问
      a (always)- 本会话内该工具名加入白名单,后续不再询问
      n (no)    - 拒绝,模型会收到"用户拒绝"消息并自行决定下一步

    Ctrl-C / Ctrl-D / EOF 时视为拒绝,安全退出。
    """

    def __init__(self) -> None:
        # 本会话的白名单:选过 a 的工具名存在这里,下次直接放行
        self._always: set[str] = set()

    def request(self, tool_name: str, tool_input: dict[str, Any]) -> bool:
        # 已在白名单中,直接放行,不再询问
        if tool_name in self._always:
            return True

        prompt = (
            f"  ? 允许执行 {tool_name}?  "
            f"[y]这次  [a]本会话总是  [n]拒绝 > "
        )
        try:
            while True:
                choice = input(prompt).strip().lower()
                if choice in ("y", "yes"):
                    return True
                if choice in ("a", "always"):
                    self._always.add(tool_name)  # 加入白名单
                    return True
                if choice in ("n", "no"):
                    return False
                # 输入了其他内容,重新提示
        except (EOFError, KeyboardInterrupt):
            print()  # 换行,避免提示符和后续输出混在一行
            return False
