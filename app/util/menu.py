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

import os
import sys
from typing import Optional, TypeVar

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style

from app.util.text import display_width, truncate_to_width

T = TypeVar("T")

_MENU_STYLE = Style.from_dict({
    "header": "fg:#ffd75f bold",     # 黄色:待审批工具、参数和风险内容
    "title": "fg:#ffd75f bold",      # 黄色:审批问题
    "selected": "fg:#5fafff bold",   # 蓝色加粗:当前光标所在选项
    "shortcut": "fg:#888888",        # 灰色:数字快捷键
    "footer": "fg:#666666 italic",   # 暗灰斜体:键位说明
})


def _get_terminal_height() -> int:
    """获取终端高度（行数）。拿不到就按 24 行保守估计。"""
    try:
        return os.get_terminal_size().lines
    except (OSError, AttributeError, ValueError):
        return 24


def _get_terminal_width() -> int:
    """获取终端宽度（列数）。拿不到就按 80 列保守估计。"""
    try:
        return max(20, os.get_terminal_size().columns)
    except (OSError, AttributeError, ValueError):
        return 80


def _visible_rows() -> int:
    """菜单可占用的行数。留 1 行余量，避免贴着屏幕底边被裁。"""
    return max(8, _get_terminal_height() - 1)


def _wrap_display_line(
    text: str,
    width: int,
    *,
    continuation_indent: str | None = None,
) -> list[str]:
    """按终端显示宽度换行，兼容中文宽字符并保留续行缩进。"""
    width = max(1, width)
    text = text.expandtabs(4)
    if display_width(text) <= width:
        return [text]

    if continuation_indent is None:
        leading = text[:len(text) - len(text.lstrip())]
        continuation_indent = leading + "  "
    continuation_indent = truncate_to_width(
        continuation_indent,
        max(0, width - 1),
        ellipsis="",
    )

    rows: list[str] = []
    remaining = text
    first = True
    while remaining:
        prefix = "" if first else continuation_indent
        room = max(1, width - display_width(prefix))
        used = 0
        end = 0
        last_space = 0
        for index, char in enumerate(remaining):
            char_width = display_width(char)
            if used + char_width > room:
                break
            used += char_width
            end = index + 1
            if char.isspace():
                last_space = end

        # 极窄终端遇到双宽字符时也必须向前推进，避免死循环。
        if end == 0:
            end = 1
        if end < len(remaining) and last_space:
            end = last_space

        chunk = remaining[:end].rstrip()
        remaining = remaining[end:].lstrip()
        rows.append(prefix + chunk)
        first = False

    return rows or [""]


def _wrap_display_lines(lines: list[str], width: int) -> list[str]:
    """展开输入中的换行，再把每个物理行限制在终端宽度内。"""
    wrapped: list[str] = []
    for source in lines:
        physical_lines = str(source).splitlines() or [""]
        for line in physical_lines:
            wrapped.extend(_wrap_display_line(line, width))
    return wrapped


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

    长内容处理:
      header 可能很长(几十行的 shell 命令)。整块菜单一旦超过终端高度,
      prompt_toolkit 的内联渲染会把超出的部分*裁掉*而不是滚动 —— 被裁掉的正好是
      底部的选项，用户就没法审批了。所以这里给"标题+选项+footer"预留固定行数
      (永远可见)，header 只拿剩下的行数做可滚动视口：内容一行不删，看不全的
      用 Ctrl+U / Ctrl+D(或 PgUp/PgDn)滚。

    在非 TTY 环境下退化:打印选项后从 stdin 读一行,接受 y/n 简化输入。
    """
    if not choices:
        return None

    # 非 TTY (管道 / 重定向):走 input() 简化路径,避免 prompt_toolkit 抛错
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        return _select_menu_fallback(choices, header_lines, title)

    # 最右侧留一列，避免部分终端在写满最后一列时自行再换一行。
    content_width = max(10, _get_terminal_width() - 1)
    header_lines = _wrap_display_lines(list(header_lines or []), content_width)
    title = truncate_to_width(title.replace("\n", " "), content_width)
    footer = truncate_to_width(footer.replace("\n", " "), content_width)

    # 标题 + 选项 + 空行 + footer 始终优先可见；header 使用剩余空间。
    fixed_rows = len(choices) + 3
    available = _visible_rows()
    # header 与交互区之间还需要一行空白，因此从预算中扣除。
    header_budget = max(0, available - fixed_rows - 1) if header_lines else 0
    scrollable = len(header_lines) > header_budget
    # 空间足够时留一行显示滚动位置；极矮终端优先展示审批内容本身。
    show_scroll_hint = scrollable and header_budget >= 2
    viewport_rows = header_budget - (1 if show_scroll_hint else 0)
    max_offset = max(0, len(header_lines) - viewport_rows)

    # 当前光标位置 / header 滚动偏移(可变,在闭包里被键位 handler 修改)
    state = {"index": 0, "result": None, "cancelled": False, "offset": 0}

    def render() -> FormattedText:
        """每次渲染都重新生成所有行(prompt_toolkit 会自动重绘)。"""
        out: list[tuple[str, str]] = []
        # header:黄色,展示待审批工具上下文
        if header_lines and header_budget:
            if scrollable:
                start = state["offset"]
                end = min(start + viewport_rows, len(header_lines))
                for line in header_lines[start:end]:
                    out.append(("class:header", line + "\n"))
                if show_scroll_hint:
                    more_above = start
                    more_below = len(header_lines) - end
                    hint = f"  ── {start + 1}-{end} / {len(header_lines)} 行"
                    if more_above:
                        hint += f" · ↑还有 {more_above}"
                    if more_below:
                        hint += f" · ↓还有 {more_below}"
                    hint += " · Ctrl+U/Ctrl+D 滚动 ──"
                    out.append((
                        "class:footer",
                        truncate_to_width(hint, content_width) + "\n",
                    ))
            else:
                for line in header_lines:
                    out.append(("class:header", line + "\n"))
            out.append(("", "\n"))
        # title:黄色加粗审批问题
        out.append(("class:title", title + "\n"))
        # choices: 当前项前 ❯ + 蓝色,其余前 2 空格
        for i, (label, _) in enumerate(choices):
            prefix = "❯ " if i == state["index"] else "  "
            number = f"{i + 1}. "
            label_width = max(1, content_width - display_width(prefix + number))
            fitted_label = truncate_to_width(
                str(label).replace("\n", " "),
                label_width,
            )
            num_style = "class:selected" if i == state["index"] else "class:shortcut"
            text_style = "class:selected" if i == state["index"] else ""
            out.append((num_style, prefix + number))
            out.append((text_style, fitted_label + "\n"))
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

    # header 滚动:方向键已经被选项占用，这里用 Ctrl+U/Ctrl+D 与 PgUp/PgDn。
    def _scroll(delta: int) -> None:
        state["offset"] = max(0, min(max_offset, state["offset"] + delta))

    @kb.add("c-u")
    @kb.add("pageup")
    def _scroll_up(event):
        _scroll(-max(1, viewport_rows // 2))

    @kb.add("c-d")
    @kb.add("pagedown")
    def _scroll_down(event):
        _scroll(max(1, viewport_rows // 2))

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

    # Window 高度必须是确定值,且不能超过终端可用行数 —— 超了 prompt_toolkit 会
    # 裁掉底部(也就是选项),而不是滚动。
    if header_lines and header_budget:
        header_rows = (
            viewport_rows + (1 if show_scroll_hint else 0)
            if scrollable
            else len(header_lines)
        )
        total_lines = header_rows + fixed_rows + 1
    else:
        total_lines = fixed_rows
    total_lines = min(total_lines, available)

    app = Application(
        layout=Layout(HSplit([
            Window(
                FormattedTextControl(render),
                height=total_lines,
                wrap_lines=False,
            )
        ])),
        key_bindings=kb,
        style=_MENU_STYLE,
        full_screen=False,
        erase_when_done=True,  # 关闭后擦掉菜单本身,只留用户后续输出
    )
    # 独占 stdin:暂停后台 Esc 监听线程,否则它会抢走菜单的按键字节,
    # 导致"选 1 / Enter 要按很多次才生效"。
    from app.util.input_arbiter import foreground_stdin
    with foreground_stdin():
        app.run()

    if state["cancelled"]:
        return None
    return state["result"]


def _select_menu_fallback(
    choices: list[tuple[str, T]],
    header_lines: Optional[list[str]],
    title: str,
) -> Optional[T]:
    """非 TTY 退化:黄色打印审批内容后从 stdin 读单行数字。"""
    yellow = "\033[1;33m" if getattr(sys.stderr, "isatty", lambda: False)() else ""
    reset = "\033[0m" if yellow else ""
    for line in (header_lines or []):
        print(f"{yellow}{line}{reset}", file=sys.stderr)
    print(f"{yellow}{title}{reset}", file=sys.stderr)
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


def prompt_text(message: str) -> Optional[str]:
    """显示文本输入提示，返回用户输入的文本；用户取消时返回 None。

    参数:
      message - 提示文本

    在非 TTY 环境下退化为 input()。
    """
    if not sys.stdout.isatty() or not sys.stdin.isatty():
        try:
            return input(f"{message} ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.key_binding import KeyBindings

    kb = KeyBindings()

    @kb.add("escape")
    @kb.add("c-c")
    def _cancel(event):
        event.app.exit(exception=KeyboardInterrupt())

    try:
        from app.util.input_arbiter import foreground_stdin
        # prompt_text 可能紧跟在 select_menu 后、且外层 Broker 已经持有前台锁；
        # foreground_stdin 可重入，整个“菜单→修改建议”流程不会让 EscWatcher 抢 stdin。
        with foreground_stdin():
            result = pt_prompt(
                message + " ",
                key_bindings=kb,
            )
        return result.strip() if result else None
    except (KeyboardInterrupt, EOFError):
        return None
