"""终端文本宽度工具。

cli 的 footer 擦除、markdown 的表格对齐都要按"终端上占几列"算宽度，
两边必须用同一套实现 —— 各写一份迟早在中日韩宽字符上对不齐。
"""
from __future__ import annotations

import re
import unicodedata

# 只匹配 SGR(颜色/粗体等)序列。注意：OSC 8 超链接这类 \033]...\033\\ 不在此列，
# 所以渲染层不要产出 OSC 序列，否则这里剥不掉，宽度会算多、footer 会错位。
_ANSI_SGR_RE = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    """去掉 SGR 转义，只留可见字符。"""
    return _ANSI_SGR_RE.sub("", text)


def display_width(text: str) -> int:
    """终端显示宽度：东亚宽字符算 2，其余算 1。先剥 ANSI，否则会把色码算进去。"""
    width = 0
    for ch in strip_ansi(text):
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def truncate_to_width(text: str, limit: int, ellipsis: str = "…") -> str:
    """按显示宽度截断（不含 ANSI 的纯文本用）。"""
    if display_width(text) <= limit:
        return text
    room = max(0, limit - display_width(ellipsis))
    out, width = [], 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if width + w > room:
            break
        out.append(ch)
        width += w
    return "".join(out) + ellipsis
