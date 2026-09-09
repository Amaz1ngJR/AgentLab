"""终端 markdown 渲染：块级结构 + 行内标记 + 代码语法高亮。

为什么是一个类而不是一个函数：
  流式路径拿到的是任意切分的增量，``` 围栏、`code`、**粗体** 都必须收齐才能
  判定，所以需要跨调用保留状态(当前是否在代码块、表格缓冲、半截的行)。
  非流式路径 render() 只是"喂一次再 flush"的包装 —— 两条路径共用同一套实现，
  避免各写一份之后慢慢分叉(之前就是这么分叉的)。

刻意不产出 OSC 8 超链接：app.util.text.strip_ansi 只剥 SGR 序列，OSC 会被算进
显示宽度，导致 cli 的 footer 擦除行数算错、出现重影。链接改用下划线 + 暗色 URL。
"""
from __future__ import annotations

import os
import re

from app.util.text import display_width, strip_ansi

RESET = "\033[0m"
BOLD = "\033[1m"
BOLD_OFF = "\033[22m"
DIM = "\033[2;90m"
ITALIC = "\033[3m"
ITALIC_OFF = "\033[23m"
UNDERLINE = "\033[4m"
UNDERLINE_OFF = "\033[24m"
STRIKE = "\033[9m"
STRIKE_OFF = "\033[29m"
WHITE = "\033[97m"
CYAN = "\033[36m"
BLUE = "\033[94m"

# ── 块级：整行判定 ────────────────────────────────────────────────────────────
FENCE_OPEN_RE = re.compile(r"^\s{0,3}```+\s*(\S*)\s*$")
FENCE_CLOSE_RE = re.compile(r"^\s{0,3}```+\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")
HR_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
UL_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
OL_RE = re.compile(r"^(\s*)(\d{1,9})[.)]\s+(.*)$")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|[\s:|-]+\|\s*$")

# ── 行内 ─────────────────────────────────────────────────────────────────────
INLINE_CODE_RE = re.compile(r"(?<!`)`([^`\n]+)`(?!`)")
LINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
# 用反向引用配对，避免 **粗体__ 这种错配也被当成一对
BOLD_RE = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
ITALIC_RE = re.compile(
    r"(?<![*\w])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![*\w])"
    r"|(?<![_\w])_(?=\S)([^_\n]+?)(?<=\S)_(?![_\w])"
)

# 行内标记的开合符号：流式时用来判断"这段能安全吐出多少"
_PAIRED_MARKS = ("```", "``", "`", "***", "**", "*", "~~", "___", "__", "_")


def unclosed_mark_pos(text: str) -> int:
    """返回第一个未闭合行内标记的位置；全部闭合则返回 len(text)。

    流式渲染靠它决定能安全输出多少 —— 标记闭合前就吐出去，正则永远匹配不到那
    一对，`**` 就只能原样打在屏幕上。未闭合的 `[` 也要压住，否则链接会被拆开。
    """
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "[":
            close = text.find(")", i)
            if close == -1:
                return i
            i = close + 1
            continue
        if ch in "`*~_":
            for mark in _PAIRED_MARKS:
                if text.startswith(mark, i):
                    close = text.find(mark, i + len(mark))
                    if close == -1:
                        return i
                    i = close + len(mark)
                    break
            else:
                i += 1
            continue
        i += 1
    return n


# ── pygments：style / formatter 构造不便宜，代码块每行都要用，缓存住 ──────────
_PYG: dict = {}


def _vscode_dark_style():
    cached = _PYG.get("style")
    if cached is not None:
        return cached
    from pygments.style import Style
    from pygments.token import (Comment, Error, Generic, Keyword, Name, Number,
                                Operator, Punctuation, String, Token)

    class _VSCodeDark(Style):
        """VS Code Dark+ 的 token 配色。"""

        background_color = "#1e1e1e"
        styles = {
            Token: "#d4d4d4",
            Comment: "#6a9955",
            Comment.Preproc: "#c586c0",
            Keyword: "#569cd6",
            Keyword.Control: "#c586c0",
            Keyword.Type: "#569cd6",
            Operator: "#d4d4d4",
            Operator.Word: "#c586c0",
            Punctuation: "#d4d4d4",
            Name: "#9cdcfe",
            Name.Builtin: "#4ec9b0",
            Name.Builtin.Pseudo: "#569cd6",
            Name.Class: "#4ec9b0",
            Name.Namespace: "#4ec9b0",
            Name.Function: "#dcdcaa",
            Name.Decorator: "#dcdcaa",
            Name.Attribute: "#9cdcfe",
            Name.Tag: "#569cd6",
            Name.Constant: "#4fc1ff",
            Name.Exception: "#4ec9b0",
            String: "#ce9178",
            String.Escape: "#d7ba7d",
            Number: "#b5cea8",
            Generic.Deleted: "#f44747",
            Generic.Inserted: "#6a9955",
            Generic.Emph: "italic",
            Generic.Strong: "bold",
            Error: "#f44747",
        }

    _PYG["style"] = _VSCodeDark
    return _VSCodeDark


def _formatter():
    """优先 truecolor：256 色近似会把 Dark+ 压得偏灰。"""
    cached = _PYG.get("formatter")
    if cached is not None:
        return cached
    style = _vscode_dark_style()
    if os.getenv("COLORTERM", "").lower() in ("truecolor", "24bit"):
        from pygments.formatters import TerminalTrueColorFormatter
        formatter = TerminalTrueColorFormatter(style=style)
    else:
        from pygments.formatters import Terminal256Formatter
        formatter = Terminal256Formatter(style=style)
    _PYG["formatter"] = formatter
    return formatter


def _lexer_for(language: str, code: str = ""):
    """stripall 必须为 False：流式一次只喂一行，开启后会把代码缩进吃掉。"""
    from pygments.lexers import TextLexer, get_lexer_by_name, guess_lexer
    from pygments.util import ClassNotFound
    if language:
        try:
            return get_lexer_by_name(language, stripall=False)
        except ClassNotFound:
            pass
    if code:
        try:
            return guess_lexer(code)
        except ClassNotFound:
            pass
    return TextLexer()


def highlight_code(code: str, language: str = "") -> str:
    """整段代码上色。失败时原样返回 —— 渲染问题不该让内容消失。"""
    try:
        from pygments import highlight
        return highlight(code, _lexer_for(language, code), _formatter()).rstrip("\n")
    except Exception:
        return code


# 块级标记全都出现在行首。流式时一旦提前把行首吐出去，就再没机会把这一行
# 渲染成标题/列表/表格了 —— 所以行首像块标记就压住等换行，其余照常逐字符流。
_BLOCK_LEAD_CHARS = "#-*+>|`~_=0123456789"


def _maybe_block_prefix(pending: str) -> bool:
    stripped = pending.lstrip(" \t")
    if not stripped:
        return True  # 目前只有缩进，可能是缩进列表
    return stripped[0] in _BLOCK_LEAD_CHARS


class MarkdownRenderer:
    """按行消费 markdown 增量，产出带 ANSI 的终端文本。

    feed() 只在收到完整行时渲染块级结构；行尾未闭合的部分会压住等下一次
    feed()。收尾必须调 flush()，因为模型末尾常常不带换行。
    """

    CODE_BAR = "│ "
    QUOTE_BAR = "▌ "

    def __init__(self, *, color: bool = True, width_fn=None, base: str = WHITE):
        self._color = color
        self._width_fn = width_fn or (lambda: 80)
        self._base = base
        self._pending = ""
        self._at_line_start = True
        self._in_code = False
        self._code_lang = ""
        self._lexer = None
        self._table: list[str] = []

    # ── 对外 ────────────────────────────────────────────────────────────────
    def feed(self, delta: str) -> str:
        if not delta:
            return ""
        if not self._color:
            return delta
        out: list[str] = []
        for ch in delta:
            self._pending += ch
            if ch != "\n":
                continue
            line = self._pending[:-1]
            self._pending = ""
            at_start = self._at_line_start
            self._at_line_start = True
            rendered = self._render_line(line, at_start)
            # None = 这一行被吞掉了(缓冲进表格 / 收尾围栏)，连换行一起不输出，
            # 否则每缓冲一行就在屏幕上留一个空行。
            if rendered is not None:
                out.append(rendered + "\n")

        # 行尾残留：代码块内与表格中必须整行才能处理，整段压住；正文只压住
        # 未闭合标记之后那一小截，前面的照常即时输出，保住流式手感。
        if self._pending and not self._in_code and not self._table:
            if not (self._at_line_start and _maybe_block_prefix(self._pending)):
                cut = unclosed_mark_pos(self._pending)
                if cut > 0:
                    out.append(self._inline(self._pending[:cut]))
                    self._pending = self._pending[cut:]
                    self._at_line_start = False
        return "".join(out)

    def flush(self) -> str:
        """吐出所有缓冲内容：未闭合的表格、代码块和最后一行残留。"""
        if not self._color:
            out, self._pending = self._pending, ""
            return out
        out: list[str] = []
        if self._pending:
            line, self._pending = self._pending, ""
            rendered = self._render_line(line, self._at_line_start)
            if rendered is not None:
                out.append(rendered)
        out.append(self._drain_table())
        self._at_line_start = True
        return "".join(out)

    # ── 块级 ────────────────────────────────────────────────────────────────
    def _render_line(self, line: str, at_start: bool) -> str | None:
        """渲染一整行。返回 None 表示这一行被吞掉了(缓冲进表格 / 收尾围栏)。"""
        if self._in_code:
            if at_start and FENCE_CLOSE_RE.match(line):
                self._in_code = False
                self._code_lang = ""
                self._lexer = None
                return None
            return self._code_line(line)

        if at_start:
            match = FENCE_OPEN_RE.match(line)
            if match:
                flushed = self._drain_table()
                self._in_code = True
                self._code_lang = match.group(1)
                self._lexer = _lexer_for(self._code_lang)
                if not self._code_lang:
                    return flushed or None
                # 语言标签使用正常颜色，更清晰
                return f"{flushed}{self._base}{self._code_lang}{RESET}"

            if TABLE_ROW_RE.match(line):
                self._table.append(line)
                return None

        flushed = self._drain_table()

        if not at_start:
            return flushed + self._inline(line)

        if HR_RE.match(line):
            # 水平线使用正常颜色
            return flushed + f"{self._base}{'─' * max(4, self._width_fn())}{RESET}"

        match = HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            color = CYAN if level <= 2 else ""
            return flushed + f"{color}{BOLD}{self._inline(match.group(2))}{BOLD_OFF}{RESET}"

        match = QUOTE_RE.match(line)
        if match:
            # 引用块使用正常颜色
            body = self._inline(match.group(1), base=self._base)
            return flushed + f"{self._base}{self.QUOTE_BAR}{body}{RESET}"

        match = OL_RE.match(line)
        if match:
            indent, number, body = match.groups()
            # 有序列表序号使用正常颜色
            return flushed + f"{indent}{self._base}{number}.{RESET}{self._base} {self._inline(body)}"

        match = UL_RE.match(line)
        if match:
            indent, body = match.groups()
            # 使用正常颜色而非 DIM，让圆点更清晰
            return flushed + f"{indent}{self._base}•{RESET}{self._base} {self._inline(body)}"

        return flushed + self._inline(line)

    def _code_line(self, line: str) -> str:
        try:
            from pygments import highlight
            body = highlight(line, self._lexer, _formatter()).rstrip("\n") if line else ""
        except Exception:
            body = line
        # 代码块左侧竖线使用正常颜色
        return f"{self._base}{self.CODE_BAR}{RESET}{body}"

    # ── 表格 ────────────────────────────────────────────────────────────────
    def _drain_table(self) -> str:
        """把缓冲的表格行按列宽对齐后一次性输出。

        必须整表缓冲：列宽要看过所有行才能定，流式一行行吐是对不齐的。
        """
        if not self._table:
            return ""
        rows = [r for r in self._table if not TABLE_SEP_RE.match(r)]
        self._table = []
        cells = [[c.strip() for c in row.strip().strip("|").split("|")] for row in rows]
        if not cells:
            return ""
        columns = max(len(r) for r in cells)
        cells = [r + [""] * (columns - len(r)) for r in cells]
        rendered = [[self._inline(c) for c in row] for row in cells]
        widths = [
            max(display_width(strip_ansi(row[i])) for row in rendered)
            for i in range(columns)
        ]

        out = []
        for index, row in enumerate(rendered):
            padded = [
                cell + " " * (widths[i] - display_width(strip_ansi(cell)))
                for i, cell in enumerate(row)
            ]
            # 表格列分隔符使用正常颜色
            sep = f"{self._base} │{RESET}{self._base} "
            out.append(f"{self._base}{sep.join(padded)}{RESET}")
            if index == 0:
                # 分隔行使用正常颜色
                out.append(self._base + "─┼─".join("─" * w for w in widths) + RESET)
        return "\n".join(out) + "\n"

    # ── 行内 ────────────────────────────────────────────────────────────────
    def _inline(self, text: str, base: str | None = None) -> str:
        """渲染行内标记。代码片段先切出来，里面的 ** 不能当粗体。"""
        base = self._base if base is None else base
        out: list[str] = []
        pos = 0
        for match in INLINE_CODE_RE.finditer(text):
            out.append(self._emphasis(text[pos:match.start()], base))
            out.append(f"{RESET}{CYAN}{match.group(1)}{RESET}{base}")
            pos = match.end()
        out.append(self._emphasis(text[pos:], base))
        return f"{base}{''.join(out)}{RESET}"

    @staticmethod
    def _emphasis(text: str, base: str) -> str:
        # 关粗体/斜体用 22/23 而不是 0 —— 整体 reset 会把这段的底色一并清掉
        text = LINK_RE.sub(
            lambda m: (
                f"{BLUE}{UNDERLINE}{m.group(1) or m.group(2)}{UNDERLINE_OFF}{RESET}"
                f"{DIM} {m.group(2)}{RESET}{base}"
            ),
            text,
        )
        text = STRIKE_RE.sub(lambda m: f"{STRIKE}{m.group(1)}{STRIKE_OFF}", text)
        text = BOLD_RE.sub(lambda m: f"{BOLD}{m.group(2)}{BOLD_OFF}", text)
        text = ITALIC_RE.sub(
            lambda m: f"{ITALIC}{m.group(1) or m.group(2)}{ITALIC_OFF}", text,
        )
        return text


def render(text: str, *, color: bool = True, width_fn=None, base: str = WHITE) -> str:
    """一次性渲染整段 markdown（非流式路径）。与流式共用同一套实现。"""
    renderer = MarkdownRenderer(color=color, width_fn=width_fn, base=base)
    return renderer.feed(text) + renderer.flush()
