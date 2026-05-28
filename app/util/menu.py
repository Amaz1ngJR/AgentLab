"""内联方向键菜单 —— 替代 input("y/n") 风格的字符提示。

使用场景:
  审批工具调用时给用户展示选项菜单,用户用 ↑↓ 移动 / Enter 确认 / 数字快捷键 /
  Esc 取消。退出时擦掉菜单本身的渲染,只在终端历史里留下用户选择的结果。

实现要点:
  - 用 prompt_toolkit Application(full_screen=False) 实现内联渲染,不会清屏
  - erase_when_done=True 让菜单关闭后自己擦掉,后续输出不会有残影
  - 数字键 1-9 是快捷键,跳过方向键直接选

参考 Claude Code 的审批弹窗样式:
    Bash command
      conda activate myenv && python -m pytest ...
    Do you want to proceed?
    > 1. Yes
      2. Yes, and don't ask again
      3. No
    Esc to cancel · Tab to amend
"""
from __future__ import annotations

import sys
from typing import Optional, TypeVar

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

T = TypeVar("T")

_MENU_STYLE = Style.from_dict({
    "header": "fg:#aaaaaa",          # 灰色:工具名 / 参数等次要信息
    "title": "bold",                  # 加粗:Do you want to proceed?
    "selected": "fg:#5fafff bold",    # 蓝色加粗:当前光标所在选项
    "shortcut": "fg:#888888",         # 灰色:数字快捷键
    "footer": "fg:#666666 italic",    # 暗灰斜体:键位说明
})


def select_menu(
    choices: list[tuple[str, T]],
    *,
    header_lines: Optional[list[str]] = None,
    title: str = "Do you want to proceed?",
    footer: str = "↑↓ 移动 · Enter 确认 · 1-9 快捷键 · Esc 取消",
) -> Optional[T]:
    """显示一个方向键菜单,返回选中的 value;用户取消时返回 None。

    参数:
      choices       - [(显示文本, 返回值), ...] 顺序即菜单顺序
      header_lines  - 选项前的描述行(例如展示工具调用的 name + arguments),
                      可以是多行,纯灰色,只用于上下文展示
      title         - 选项上方的提问句
      footer        - 选项下方的键位提示

    在非 TTY 环境下退化:打印选项后从 stdin 读一行,接受 y/n 简化输入。
    """
    if not choices:
        return None

    # 非 TTY (管道 / 重定向): 走 input() 简化路径,避免 prompt_toolkit 抛错
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return _select_menu_fallback(choices, header_lines, title)

    # 当前光标位置(可变,在闭包里被键位 handler 修改)
    state = {"index": 0, "result": None, "cancelled": False}

    def render() -> FormattedText:
        """每次渲染都重新生成所有行(prompt_toolkit 会自动重绘)。"""
        out: list[tuple[str, str]] = []
        # header: 灰色,展示工具上下文
        for line in (header_lines or []):
            out.append(("class:header", line + "\n"))
        if header_lines:
            out.append(("", "\n"))
        # title: 加粗
        out.append(("class:title", title + "\n"))
        # choices: 当前项前 ❯ + 蓝色,其余前 2 空格
        for i, (label, _) in enumerate(choices):
            prefix = "❯ " if i == state["index"] else "  "
            num_style = "class:selected" if i == state["index"] else "class:shortcut"
            text_style = "class:selected" if i == state["index"] else ""
            out.append((num_style, f"{prefix}{i + 1}. "))
            out.append((text_style, f"{label}\n"))
        # footer: 暗灰斜体
        out.append(("", "\n"))
        out.append(("class:footer", footer))
        return FormattedText(out)

    kb = KeyBindings()

    @kb.add("up")
    @kb.add("c-p")
    @kb.add("k")
    def _up(event):
        state["index"] = (state["index"] - 1) % len(choices)

    @kb.add("down")
    @kb.add("c-n")
    @kb.add("j")
    def _down(event):
        state["index"] = (state["index"] + 1) % len(choices)

    @kb.add("enter")
    def _enter(event):
        state["result"] = choices[state["index"]][1]
        event.app.exit()

    @kb.add("escape", eager=True)
    @kb.add("c-c")
    def _cancel(event):
        state["cancelled"] = True
        event.app.exit()

    # 数字键 1-9: 直接选定对应项(无需先按方向键)
    for i in range(min(len(choices), 9)):
        digit = str(i + 1)

        @kb.add(digit)
        def _digit(event, idx=i):
            state["result"] = choices[idx][1]
            event.app.exit()

    # 计算菜单总行数(给 Window height 用);prompt_toolkit 需要确定的行数
    # 才能正确预留终端空间
    total_lines = len(choices) + 2  # title + 选项 + footer 前空行
    if header_lines:
        total_lines += len(header_lines) + 1
    total_lines += 2  # footer 行 + footer 前空行

    app = Application(
        layout=Layout(HSplit([
            Window(FormattedTextControl(render), height=total_lines)
        ])),
        key_bindings=kb,
        style=_MENU_STYLE,
        full_screen=False,
        erase_when_done=True,  # 关闭后擦掉菜单本身,只留用户后续输出
    )
    app.run()

    if state["cancelled"]:
        return None
    return state["result"]


def _select_menu_fallback(
    choices: list[tuple[str, T]],
    header_lines: Optional[list[str]],
    title: str,
) -> Optional[T]:
    """非 TTY 退化:打印选项后从 stdin 读单行数字。"""
    for line in (header_lines or []):
        print(line, file=sys.stderr)
    print(title, file=sys.stderr)
    for i, (label, _) in enumerate(choices):
        print(f"  {i + 1}. {label}", file=sys.stderr)
    try:
        raw = input(f"[1-{len(choices)}] > ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    try:
        idx = int(raw) - 1
    except ValueError:
        return None
    if 0 <= idx < len(choices):
        return choices[idx][1]
    return None
