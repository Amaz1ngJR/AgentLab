"""AgentLab CLI —— 程序入口和用户界面。

用法:
    agentlab --workspace .                     # 安装后从任意目录启动
    python -m app                              # 交互式对话
    python -m app -p "帮我看 README.md"        # 单次 prompt 后退出
    python -m app -y                           # 自动放行所有工具调用
    python -m app --profile cloud_claude       # 使用指定 profile

退出: exit / quit / Ctrl-D
中断执行: Esc 或 Ctrl-C(停下后可直接输入新指令调整方向)
重置会话: /reset
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style

from app.agent.approval import AutoApprove, InteractivePolicy
from app.attachments import (
    AttachmentError,
    AttachmentStore,
    ImageAttachment,
    capture_system_clipboard,
)
from app.agent.approval_broker import ApprovalBroker, BrokerApprovalPolicy
from app.agent.cancel import CancelToken
from app.agent.context import ContextManager
from app.agent.context_budget import ContextBudget
from app.agent.context_compaction import ContextCompressor
from app.agent.events import RunEvent
from app.agent import events as run_events
from app.agent.planner import Planner
from app.agent.profiles import load_agent_profiles
from app.agent.runtime import AgentSession, TurnEvent, build_system_prompt
from app.agent.session_router import SessionRouter
from app.agent.service import RuntimeService
from app.agent.tasks import TaskStore
from app.config.loader import load_config, workspace_root
from app.mcp.adapter import build_mcp_tools
from app.mcp.config import enabled_servers
from app.mcp.manager import MCPManager
from app.memory import build_memory_policy, inject_memories
from app.models.router import build_model_router
from app.skills import SkillCatalog
from app.storage import Storage
from app.tools.builtin import default_tools
from app.tools.builtin.interactive import PtySessionManager, make_terminal_tools
from app.tools.builtin.todo import make_todo_write_tool
from app.tools.registry import ToolRegistry
from app.util.redact import format_exception, format_traceback
from app.version import __version__, version_text

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _term_width(default: int = 80) -> int:
    try:
        cols = shutil.get_terminal_size((default, 24)).columns
    except (OSError, ValueError):
        return default
    # 某些 PTY (例如 script 命令) 会返回 0,需要兜底为合理默认
    return cols if cols > 0 else default


def _display_width(text: str) -> int:
    """终端显示宽度：东亚宽字符算 2，其他算 1。

    先剥掉 ANSI 转义(dim 思考流 / 任务面板高亮都带颜色码),否则会把不可见的
    控制字符算进宽度,导致 footer 擦除时上移行数算错、出现重影。
    """
    text = _strip_ansi(text)
    width = 0
    for ch in text:
        if unicodedata.east_asian_width(ch) in ("W", "F"):
            width += 2
        else:
            width += 1
    return width


def _count_visual_lines(text: str, term_width: int) -> int:
    """按终端宽度计算文本占用的实际屏幕行数（考虑 \\n 与软换行）。"""
    if not text:
        return 0
    lines = text.rstrip("\n").split("\n") if text else []
    n = 0
    for line in lines:
        w = _display_width(line)
        n += max(1, (w + term_width - 1) // term_width) if w else 1
    return n


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


def _fmt_tokens(n: int) -> str:
    if n < 1000:
        return f"{n} tokens"
    return f"{n / 1000:.1f}k tokens"


# 任务列表渲染用的 ANSI 颜色 / 字符
# 不用 prompt_toolkit Style 是为了让任务列表跟 spinner / 文本走同一条 stdout 流,
# 避免分别用 prompt_toolkit Application 与 ANSI 写入的协调问题
_ANSI_RESET = "\033[0m"
_ANSI_WHITE = "\033[97m"         # 白色 (模型正式输出)
_ANSI_GREEN = "\033[32m"         # 绿色 (启动初始化)
_ANSI_DIM = "\033[2;90m"        # 灰色 dim (思考 / completed)
_ANSI_BLUE_BOLD = "\033[1;34m"  # 蓝色加粗 (in_progress 高亮)
_ANSI_BOLD = "\033[1m"           # 加粗 (汇总行)
_ANSI_YELLOW = "\033[33m"        # 黄色 (blocked)
_ANSI_RED = "\033[31m"           # 红色 (failed)
_ANSI_YELLOW_BOLD = "\033[1;33m" # 黄色加粗 (执行审批)


def _supports_color(stream=None) -> bool:
    """仅在交互式终端启用颜色，并遵守 NO_COLOR / dumb 终端约定。"""
    stream = stream or sys.stdout
    if os.getenv("NO_COLOR") is not None or os.getenv("TERM", "").lower() == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def _colorize(text: str, ansi: str, *, stream=None) -> str:
    return f"{ansi}{text}{_ANSI_RESET}" if _supports_color(stream) else text


def _model_text(text: str, *, stream=None) -> str:
    """正式模型输出使用白色，与灰色思考过程区分。"""
    return _colorize(text, _ANSI_WHITE, stream=stream)


def _thinking_text(text: str, *, stream=None) -> str:
    return _colorize(text, _ANSI_DIM, stream=stream)


def _approval_text(text: str, *, stream=None) -> str:
    """待审批工具调用使用黄色，审批菜单选项仍由 prompt_toolkit 保持蓝色。"""
    return _colorize(text, _ANSI_YELLOW_BOLD, stream=stream)


def _print_init(text: str = "", *, flush: bool = False) -> None:
    """渲染启动初始化信息；每行单独 reset，避免颜色泄漏到交互提示。"""
    print(_colorize(text, _ANSI_GREEN), flush=flush)


def _task_field(t, name: str):
    """从任务项读字段,兼容 Task dataclass(属性)和 snapshot dict(键)。"""
    if isinstance(t, dict):
        return t.get(name, "")
    return getattr(t, name, "")


def _truncate_task_content(content: str, max_chars: int = 60) -> str:
    """任务面板每行只占一行:超长 content(如退化单任务把整段 prompt 当任务)截断。

    取首行(模型偶尔把多行塞进 content),再按字符数截断加省略号,避免面板被
    整段用户问题撑成多行刷屏。
    """
    if not content:
        return content
    first_line = content.splitlines()[0].strip()
    if len(first_line) > max_chars:
        return first_line[:max_chars].rstrip() + "…"
    return first_line


def _format_task_lines(tasks) -> list[str]:
    """把任务列表渲染成多行字符串(已包含 ANSI 颜色)。

    既接受 TaskStore.all() 的 Task 对象(spinner 面板用),也接受
    TaskStore.snapshot() 的 dict 列表(RunEvent 收尾面板用)。

    格式参考 Claude Code 的任务面板:
        4 tasks (1 done, 1 in progress, 2 open)
          ✓ workspace 路径限制         (灰色,已完成)
          ❯ 错误脱敏                    (蓝色加粗,进行中)
          ○ 工具能力声明                (普通色,待办)
          ⊘ 被阻塞的任务                (黄色,blocked)
          ✗ 失败的任务                  (红色,failed)

    返回的每一行 *不* 含末尾换行,调用方决定怎么拼。
    任务为空时返回空列表(让调用方决定是否显示标题区)。
    """
    if not tasks:
        return []

    counts = {"completed": 0, "in_progress": 0, "pending": 0, "blocked": 0, "failed": 0}
    for t in tasks:
        st = _task_field(t, "status")
        if st in counts:
            counts[st] += 1

    parts = [
        f"{counts['completed']} done",
        f"{counts['in_progress']} in progress",
        f"{counts['pending']} open",
    ]
    if counts["blocked"]:
        parts.append(f"{counts['blocked']} blocked")
    if counts["failed"]:
        parts.append(f"{counts['failed']} failed")
    header = (
        f"{_ANSI_BOLD}{len(tasks)} tasks{_ANSI_RESET} ({', '.join(parts)})"
    )
    lines = [header]

    for t in tasks:
        st = _task_field(t, "status")
        content = _task_field(t, "content")
        content = _truncate_task_content(content)
        if st == "completed":
            lines.append(f"  {_ANSI_DIM}✓ {content}{_ANSI_RESET}")
        elif st == "in_progress":
            lines.append(f"  {_ANSI_BLUE_BOLD}❯ {content}{_ANSI_RESET}")
        elif st == "blocked":
            lines.append(f"  {_ANSI_YELLOW}⊘ {content}{_ANSI_RESET}")
        elif st == "failed":
            lines.append(f"  {_ANSI_RED}✗ {content}{_ANSI_RESET}")
        else:
            lines.append(f"  ○ {content}")

    return lines


def _strip_ansi(s: str) -> str:
    """计算显示宽度时要先去掉 ANSI 转义,否则会把控制字符当成可见字符宽度。"""
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


class _Spinner:
    """常驻底部状态行(footer) + 实时 token 计数 + 流式文本输出 + 任务列表面板。

    渲染策略(关键:footer 贯穿整个 turn 常驻屏幕最底部,正文在其上方滚动):
      1. 任务面板:每步内固定不变(模型用 todo_write 的更新发生在 create_message
         返回之后),首次绘制时一次性提交到滚动历史,之后不再重绘。
      2. footer:始终钉在屏幕最底部,持续闪动 + 实时刷新 token 计数(thinking
         阶段显示 ↑input,生成阶段显示 ↓output),直到 turn 结束才撤掉。
      3. 正文到达:先擦掉 footer,把 delta append 到 stdout(终端自然滚动),
         再在新位置重画 footer。这样 token 在整个 turn 全程实时刷新。

    擦除的 *有界性* —— 这是不重影的关键:每次擦除只回收 footer 自己 + 当前那条
    还没换行的正文行(_line_buf),两者都至多几行,相对光标上移绝不越屏。已经
    换行滚走的正文永远不碰,所以不会出现旧实现"重打整段缓冲"导致的滚屏叠影。
    """

    def __init__(self, label: str, task_store=None, panel_state=None):
        self.label = label
        self._task_store = task_store  # None = 不显示任务面板
        # 跨 spinner 共享的面板去重状态({"last": <上次提交的面板签名>});
        # None 时退化为本 spinner 内自己去重(_tasks_committed)
        self._panel_state = panel_state
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._t0 = 0.0
        self._lock = threading.Lock()
        self._metrics: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._any_text = False         # 整个 turn 是否出现过正文(决定退出收尾)
        self._any_thinking = False     # 是否出现过思考流(用于在思考→正文/摘要间分隔)
        self._line_buf = ""            # 当前还没换行的正文尾行(擦除/重画 footer 时要复原)
        self._tasks_committed = False  # 任务面板每步只提交一次
        self._frame_idx = 0
        self._footer_rows = 0          # 当前 footer 占用的屏幕行数(0 = 未绘制)

    def __enter__(self) -> "_Spinner":
        self._t0 = time.monotonic()
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        with self._lock:
            self._erase_footer()
            self._commit_tasks()
            if self._any_text:
                # 正文已逐段 append 到历史:补足结尾换行 + 一个空行,不再打摘要
                if self._line_buf:
                    sys.stdout.write("\n")
                sys.stdout.write("\n")
            else:
                # 纯 tool_use / 无文本:保留任务面板 + 一行摘要。
                # 若只流过思考(没有正文),先换行收掉暗色尾行再打摘要。
                if self._any_thinking and self._line_buf:
                    sys.stdout.write("\n")
                sys.stdout.write(f"  ✻ {self.label} ({self._fmt_status()})\n")
            sys.stdout.flush()

    def update(self, metrics: dict[str, int]) -> None:
        with self._lock:
            self._metrics = {
                "input_tokens": int(metrics.get("input_tokens", 0) or 0),
                "output_tokens": int(metrics.get("output_tokens", 0) or 0),
            }
            self._render()

    def on_text(self, delta: str) -> None:
        if not delta:
            return
        with self._lock:
            self._erase_footer()
            self._commit_tasks()
            # 思考流→正文的过渡:思考是暗色,正文是常色。换行收掉思考尾行 +
            # 空一行分隔,避免正文紧贴在灰字后面。只在首次出现正文时做一次。
            if self._any_thinking and not self._any_text:
                if self._line_buf:
                    sys.stdout.write("\n")
                sys.stdout.write("\n")
                self._line_buf = ""
            self._any_text = True
            styled = _model_text(delta)
            sys.stdout.write(styled)
            # 维护"当前未换行尾行":有换行则取最后一段,否则累加到现有尾行。
            # _line_buf 要保留颜色,因为 footer 重绘时会重新输出这段正文。
            if "\n" in delta:
                tail = delta.rsplit("\n", 1)[1]
                self._line_buf = _model_text(tail) if tail else ""
            else:
                self._line_buf += styled
            self._draw_footer()
            sys.stdout.flush()

    def on_thinking(self, delta: str) -> None:
        """流式打印思考(推理)过程,用暗色与正式答案区分。

        复用 on_text 的"擦 footer → append → 重画 footer"机制,但把每段增量包进
        dim 颜色码。_line_buf 里存的是带色码的字符串;footer 擦除靠 _display_width
        (已剥 ANSI)算宽度,所以色码不会让上移行数算错。
        """
        if not delta:
            return
        with self._lock:
            self._erase_footer()
            self._commit_tasks()
            if not self._any_thinking:
                # 思考段起一个灰色标题,提示这是推理而非最终答案
                sys.stdout.write(_thinking_text("💭 思考\n"))
                self._any_thinking = True
            styled = _thinking_text(delta)
            sys.stdout.write(styled)
            if "\n" in delta:
                tail = delta.rsplit("\n", 1)[1]
                self._line_buf = _thinking_text(tail) if tail else ""
            else:
                self._line_buf += styled
            self._draw_footer()
            sys.stdout.flush()

    def _fmt_status(self) -> str:
        elapsed = time.monotonic() - self._t0
        out_t = self._metrics["output_tokens"]
        in_t = self._metrics["input_tokens"]
        if out_t > 0:
            return f"{_fmt_duration(elapsed)} · ↓ {_fmt_tokens(out_t)}"
        if in_t > 0:
            return f"{_fmt_duration(elapsed)} · ↑ {_fmt_tokens(in_t)}"
        return _fmt_duration(elapsed)

    def _task_lines(self) -> list[str]:
        """从 task_store 取最新快照,渲染成行列表。store=None 或空时返回 []。"""
        if self._task_store is None:
            return []
        return _format_task_lines(self._task_store.all())

    def _commit_tasks(self) -> None:
        """一次性把任务面板打印到滚动历史(每步只调一次)。空任务则什么都不打。

        面板在一个步骤内不会变(todo_write 的更新发生在 create_message 之后),
        所以提交一次即可,无需像旧实现那样每帧重绘。调用前必须已 _erase_footer,
        否则会把面板打在 footer 中间。

        去重:一次 chat 会起多个 spinner(planning / thinking / compacting),
        若每个都提交一次面板就会重复刷屏。用跨 spinner 共享的 _panel_state 记住
        上次已提交的面板签名,内容没变就跳过,只在任务状态真正变化时才重新打印。
        """
        if self._tasks_committed:
            return
        self._tasks_committed = True
        task_lines = self._task_lines()
        if not task_lines:
            return
        # 跨 spinner 去重:面板签名与上次相同则不重复提交
        if self._panel_state is not None:
            signature = "\n".join(task_lines)
            if signature == self._panel_state.get("last"):
                return
            self._panel_state["last"] = signature
        for line in task_lines:
            sys.stdout.write(line + "\n")
        sys.stdout.write("\n")  # 面板与下方内容留一行间隔
        sys.stdout.flush()

    def _render(self) -> None:
        """擦旧 footer → (首次)提交任务面板 → 重画 footer。供动画 / token 刷新调用。"""
        self._erase_footer()
        self._commit_tasks()
        self._draw_footer()
        sys.stdout.flush()

    def _draw_footer(self) -> None:
        """在正文下方画 footer,记录其占用行数,供 _erase_footer 复原。

        若当前有未换行尾行(_line_buf),先写一个 \\n 把 footer 推到下一行,避免
        footer 跟正文挤在同一行;否则光标已在行首,直接画。这个 \\n 只是 _line_buf
        与 footer 之间的边界,不额外占一整屏行 —— 擦除时不计入上移行数。
        """
        term_width = _term_width()
        if self._line_buf:
            sys.stdout.write("\n")
        frame = _SPINNER_FRAMES[self._frame_idx % len(_SPINNER_FRAMES)]
        spinner_line = f"  {frame} {self.label}… ({self._fmt_status()})"
        sys.stdout.write(spinner_line)
        w = _display_width(spinner_line)
        self._footer_rows = max(1, (w + term_width - 1) // term_width)

    def _erase_footer(self) -> None:
        """擦掉 footer,并把光标复原到正文尾行末尾,准备继续 append。

        光标此刻在 footer 末行末尾。从正文尾行(_line_buf)起点到 footer 末行,
        共占 lb_rows + footer_rows 个屏幕行(_draw_footer 里那个 \\n 只是边界,
        不额外占一整屏行),光标在最后一行,故上移 lb_rows + footer_rows - 1
        行即到正文尾行行首,清屏到末,再重打 _line_buf 把光标送回正文末尾。

        所有相对上移都 *有界*(footer 1~2 行 + 一条正文行),即使屏幕滚动也不越界
        —— 已换行滚走的正文从不触碰,这正是它稳、而旧实现重打整段文本会失效的原因。
        """
        if self._footer_rows <= 0:
            return
        term_width = _term_width()
        lb_rows = 0
        if self._line_buf:
            w = _display_width(self._line_buf)
            lb_rows = max(1, (w + term_width - 1) // term_width)
        up = lb_rows + self._footer_rows - 1
        sys.stdout.write("\r")
        if up > 0:
            sys.stdout.write(f"\033[{up}A")
        sys.stdout.write("\033[J")
        if self._line_buf:
            sys.stdout.write(self._line_buf)
        self._footer_rows = 0

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            with self._lock:
                self._frame_idx += 1
                self._render()


class _PlainProgress:
    """非 TTY 时的退化实现 —— 不打动画，但仍支持流式文本和最终 token 摘要。"""

    def __init__(self, label: str):
        self.label = label
        self._t0 = 0.0
        self._metrics: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._text_started = False
        self._thinking_started = False

    def __enter__(self) -> "_PlainProgress":
        self._t0 = time.monotonic()
        print(f"  ✻ {self.label}…", flush=True)
        return self

    def __exit__(self, *_) -> None:
        elapsed = time.monotonic() - self._t0
        out_t = self._metrics["output_tokens"]
        suffix = f" · ↓ {_fmt_tokens(out_t)}" if out_t else ""
        if self._text_started or self._thinking_started:
            sys.stdout.write("\n\n")
            sys.stdout.flush()
        print(f"  ✻ {self.label} ({_fmt_duration(elapsed)}{suffix})", flush=True)

    def update(self, metrics: dict[str, int]) -> None:
        self._metrics = {
            "input_tokens": int(metrics.get("input_tokens", 0) or 0),
            "output_tokens": int(metrics.get("output_tokens", 0) or 0),
        }

    def on_thinking(self, delta: str) -> None:
        if not delta:
            return
        if not self._thinking_started:
            self._thinking_started = True
            sys.stdout.write("\n" + _thinking_text("💭 思考\n"))
        sys.stdout.write(_thinking_text(delta))
        sys.stdout.flush()

    def on_text(self, delta: str) -> None:
        if not delta:
            return
        if not self._text_started:
            self._text_started = True
            # 思考流后接正文:先空一行把灰字推开
            sys.stdout.write("\n\n" if self._thinking_started else "\n")
        sys.stdout.write(_model_text(delta))
        sys.stdout.flush()


def _make_progress(task_store=None):
    """构造 progress 工厂(传给 AgentSession.progress)。

    把 task_store 通过闭包注入到 _Spinner,让它在重绘时能拿到最新任务列表。
    分成工厂是因为 AgentSession 需要的 progress 签名是 Callable[[label], CM],
    不能直接接受 task_store 参数。

    一次 chat 内会创建多个 spinner(planning / 每个 task 的 thinking /
    compacting),若每个 spinner 都提交一次任务面板,面板就被重复打印 5~6 次。
    用一个跨 spinner 共享的 `panel_state` 记住"上次提交的面板签名",只有面板
    内容真正变化时才重新提交,消除重复噪音。

    返回 (progress_fn, panel_state):progress_fn 供 AgentSession 用,panel_state
    供 _print_run_event 等外部路径复用同一去重状态。
    """
    panel_state = {"last": None}  # 跨 spinner 共享:上次已提交的面板签名

    @contextmanager
    def _progress(label: str):
        if sys.stdout.isatty():
            with _Spinner(label, task_store=task_store, panel_state=panel_state) as s:
                yield s
        else:
            with _PlainProgress(label) as p:
                yield p
    return _progress, panel_state


# 兼容旧调用:模块顶层仍提供一个无 task_store 的默认 _progress
_progress = _make_progress(None)


def _fmt(data: dict) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(data)


def _resolve_ws_path(path_str: str):
    """审批前只解析 workspace 内路径，禁止预览阶段读取外部文件。"""
    try:
        from app.tools.builtin.files import _resolve_within_workspace
        return _resolve_within_workspace(path_str)
    except Exception:
        return None


def _render_pre_approval_diff(path_str: str, old_content: str, new_content: str,
                              is_new: bool) -> None:
    """审批前渲染彩色 diff + 拉 VS Code diff。old/new 已知,不需执行工具。"""
    from app.util import diff as _diff

    if old_content == new_content:
        return
    is_delete = bool(old_content) and not new_content
    added, removed = _diff.diff_stats(old_content, new_content)
    width = min(_term_width(), 120)
    print(flush=True)
    print(_diff.render_header(path_str, is_new, added, removed, is_delete=is_delete),
          flush=True)
    body = _diff.render_color_diff(old_content, new_content, width=width)
    if body:
        print(body, flush=True)
    print(flush=True)
    # 记住句柄:用户允许/拒绝后关掉这个 diff tab(见 _close_active_vscode_diff)
    _register_vscode_diff(_diff.launch_vscode_diff(old_content, new_content, path_str))


# 当前打开着的 VS Code diff 句柄(edit_file/write_file 审批期间)。
# 用户做出选择后由 _close_active_vscode_diff() terminate 掉,关闭 diff tab。
_ACTIVE_VSCODE_DIFFS: list = []


def _register_vscode_diff(handle) -> None:
    if handle is not None:
        _ACTIVE_VSCODE_DIFFS.append(handle)


def _close_active_vscode_diff() -> None:
    """关闭审批期间打开的所有 VS Code diff tab(允许/拒绝后调用)。"""
    from app.util import diff as _diff
    while _ACTIVE_VSCODE_DIFFS:
        _diff.close_vscode_diff(_ACTIVE_VSCODE_DIFFS.pop())


def _render_write_file(tool_input: dict) -> None:
    """把 write_file 的改动渲染成彩色 diff 打到终端(在审批菜单之前调用)。

    读旧内容(文件不存在则视为新建),和 tool_input.content 做行级 diff。
    此刻磁盘上还是旧内容,新内容在 tool_input 里,所以同时具备旧/新两份。
    任何异常都吞掉,退化为不打 diff —— diff 是锦上添花,不该打断工具流程。
    """
    try:
        path_str = tool_input.get("path", "")
        new_content = tool_input.get("content", "") or ""
        if not path_str:
            return
        path = _resolve_ws_path(path_str)
        if path is None:
            return
        is_new = not path.exists()
        old_content = "" if is_new else path.read_text(encoding="utf-8", errors="replace")
        _render_pre_approval_diff(path_str, old_content, new_content, is_new)
    except Exception:
        return


def _render_edit_file(tool_input: dict) -> None:
    """把 edit_file 的局部替换渲染成彩色 diff(审批前调用)。

    在内存里模拟 _edit_file 的替换逻辑(old_str→new_str,空 old_str=末尾追加),
    算出替换后的完整内容,与旧内容做 diff。不真正写盘。old_str 不唯一 / 找不到
    时不显示 diff(交给工具执行时报错给模型)。
    """
    try:
        path_str = tool_input.get("path", "")
        if not path_str:
            return
        path = _resolve_ws_path(path_str)
        if path is None:
            return
        if not path.exists() or not path.is_file():
            return
        old_content = path.read_text(encoding="utf-8", errors="replace")
        old_str = tool_input.get("old_str", "")
        new_str = tool_input.get("new_str", "")
        if old_str == "":
            new_content = old_content + new_str
        else:
            if old_content.count(old_str) != 1:
                return  # 不唯一 / 未命中:执行时会报错,这里不猜
            new_content = old_content.replace(old_str, new_str, 1)
        _render_pre_approval_diff(path_str, old_content, new_content, is_new=False)
    except Exception:
        return


# shell 命令改文件的"执行前快照"。
# _SHELL_TREE_BEFORE:执行前整棵 workspace 的文件内容快照(不依赖解析命令,
#   任何写文件方式 —— 重定向 / sed / python -c / heredoc —— 落盘后都能比对出来)。
#   仓库过大时为 None,退化到窄路径 _SHELL_TARGETS(靠命令解析)。
_SHELL_TREE_BEFORE: dict[str, str] | None = None
_SHELL_TARGETS: dict[str, str] = {}


def _snapshot_shell_targets(tool_input: dict) -> None:
    """shell 执行前:抓拍 workspace 快照;仓库过大时退化为解析命令目标。"""
    global _SHELL_TREE_BEFORE
    from app.util import diff as _diff

    _SHELL_TREE_BEFORE = None
    _SHELL_TARGETS.clear()
    try:
        from app.config.loader import workspace_root
        root = workspace_root()
        # 首选:整棵树快照(与命令写法无关)
        _SHELL_TREE_BEFORE = _diff.snapshot_tree(root)
        if _SHELL_TREE_BEFORE is not None:
            return
        # 退化:仓库太大,只快照命令里能解析出的写入目标
        command = tool_input.get("command", "") or ""
        from pathlib import Path
        for t in _diff.shell_write_targets(command):
            p = Path(t)
            if not p.is_absolute():
                p = root / p  # shell 的 cwd 是 workspace_root
            _SHELL_TARGETS[str(p)] = _diff.read_text_safe(p)
    except Exception:
        _SHELL_TREE_BEFORE = None
        _SHELL_TARGETS.clear()


def _emit_file_diff(path_str, old, new, is_new, width) -> None:
    """打印单个文件的头 + 彩色 diff,并(可选)拉 VS Code diff。"""
    from app.util import diff as _diff

    is_delete = bool(old) and not new
    added, removed = _diff.diff_stats(old, new)
    print(flush=True)
    print(_diff.render_header(path_str, is_new, added, removed, is_delete=is_delete),
          flush=True)
    body = _diff.render_color_diff(old, new, width=width)
    if body:
        print(body, flush=True)
    print(flush=True)
    _diff.launch_vscode_diff(old, new, path_str)


def _render_shell_diff() -> None:
    """shell 执行后:比对执行前快照,把真正落盘的改动打成彩色 diff。"""
    import os as _os

    from app.util import diff as _diff

    _MAX_FILES_SHOWN = 10  # 一条命令改动过多文件时,只详列前 N 个
    try:
        width = min(_term_width(), 120)
        change_count = 0
        if _SHELL_TREE_BEFORE is not None:
            # 全量快照路径:再拍一次,diff 出所有变化的文件
            from app.config.loader import workspace_root
            after = _diff.snapshot_tree(workspace_root())
            changes = _diff.diff_snapshots(_SHELL_TREE_BEFORE, after)
            change_count = len(changes)
            for path_str, old, new, is_new in changes[:_MAX_FILES_SHOWN]:
                _emit_file_diff(path_str, old, new, is_new, width)
            extra = len(changes) - _MAX_FILES_SHOWN
            if extra > 0:
                print(f"  … 另有 {extra} 个文件改动(未详列)", flush=True)
        elif _SHELL_TARGETS:
            # 窄路径:只比对解析出的目标
            for path_str, old in _SHELL_TARGETS.items():
                new = _diff.read_text_safe(path_str)
                if new == old:
                    continue
                is_new = (old == "") and _os.path.exists(path_str)
                _emit_file_diff(path_str, old, new, is_new, width)
                change_count += 1
        # 命令跑了但没改任何文件:明确告知(避免用户疑惑"diff 去哪了")
        if change_count == 0 and (_SHELL_TREE_BEFORE is not None or _SHELL_TARGETS):
            print(f"  {_ANSI_DIM}(本次命令未改动任何文件){_ANSI_RESET}", flush=True)
    except Exception:
        pass
    finally:
        _reset_shell_snapshot()


def _reset_shell_snapshot() -> None:
    global _SHELL_TREE_BEFORE
    _SHELL_TREE_BEFORE = None
    _SHELL_TARGETS.clear()


def _print_event(ev: TurnEvent) -> None:
    if ev.kind == "tool_call":
        if ev.tool_name == "write_file":
            _render_write_file(ev.tool_input or {})
        elif ev.tool_name == "edit_file":
            _render_edit_file(ev.tool_input or {})
        elif ev.tool_name == "shell":
            _snapshot_shell_targets(ev.tool_input or {})
        print(_approval_text(f"  ! tool_use {ev.tool_name}({_fmt(ev.tool_input)})"), flush=True)
    elif ev.kind == "tool_result":
        preview = (ev.tool_output.splitlines()[:1] or [""])[0][:120]
        tag = "ERR" if ev.tool_error else "ok"
        t = f" ({ev.elapsed_seconds * 1000:.0f}ms)" if ev.elapsed_seconds < 1 else f" ({ev.elapsed_seconds:.1f}s)"
        print(f"    [{tag}]{t} {preview}", flush=True)
        if ev.tool_name == "shell" and not ev.tool_error:
            _render_shell_diff()
        # write_file/edit_file 允许后关闭 VS Code diff
        if ev.tool_name in ("write_file", "edit_file") and not ev.tool_error:
            _close_active_vscode_diff()
    elif ev.kind == "tool_denied":
        print(_approval_text(f"    [denied] 已拒绝 {ev.tool_name}"), flush=True)
        # write_file/edit_file 拒绝后也关闭 VS Code diff
        if ev.tool_name in ("write_file", "edit_file"):
            _close_active_vscode_diff()
    elif ev.kind == "text":
        print(f"\n{_model_text(ev.text)}\n", flush=True)


def _print_run_event(ev: RunEvent, panel_state: dict | None = None) -> None:
    """把编排路径的 RunEvent 渲染到终端。

    分工(与 spinner 配合,见 6.1):
      - message_delta:模型文本若没被 spinner 流式打印过(text_streamed=False),
        在这里补打;已流式过的不会再发 message_delta,避免重复。
      - tool_requested / tool_completed / tool_denied:工具调用在 spinner 退出后
        发生,直接打到 stdout。
      - plan_created / task_started:轻量进度提示。
      - task_updated:不单独打行(spinner 面板已实时反映任务状态)。
      - run_completed / run_failed:打最终任务面板(snapshot)+ 失败原因。

    panel_state 供去重:与 spinner 内部的 _commit_tasks 共享同一签名字典,
    避免 RUN_COMPLETED/RUN_FAILED 又重复打印面板。
    """
    kind = ev.kind
    if kind == run_events.MESSAGE_DELTA:
        if ev.text:
            print(f"\n{_model_text(ev.text)}\n", flush=True)
    elif kind == run_events.TOOL_REQUESTED:
        # 这里只做审批前 diff / shell 快照；黄色 tool_use 由紧随其后的
        # APPROVAL_REQUIRED 统一输出，免审批工具不冒充审批内容。
        if ev.tool_name == "write_file":
            _render_write_file(ev.tool_input or {})
        elif ev.tool_name == "edit_file":
            _render_edit_file(ev.tool_input or {})
        elif ev.tool_name == "shell":
            _snapshot_shell_targets(ev.tool_input or {})
    elif kind == run_events.APPROVAL_REQUIRED:
        action = ev.payload.get("approval_action") or ev.tool_name
        detail = f"  ! tool_use {action}: {ev.tool_name}({_fmt(ev.tool_input)})"
        print(_approval_text(detail), flush=True)
    elif kind == run_events.TOOL_COMPLETED:
        preview = (ev.tool_output.splitlines()[:1] or [""])[0][:120]
        tag = "ERR" if ev.tool_error else "ok"
        t = (f" ({ev.elapsed_seconds * 1000:.0f}ms)" if ev.elapsed_seconds < 1
             else f" ({ev.elapsed_seconds:.1f}s)")
        print(f"    [{tag}]{t} {preview}", flush=True)
        if ev.tool_name == "shell" and not ev.tool_error:
            _render_shell_diff()
        # write_file/edit_file 允许后关闭 VS Code diff(与 Claude Code 对齐)
        if ev.tool_name in ("write_file", "edit_file") and not ev.tool_error:
            _close_active_vscode_diff()
    elif kind == run_events.TOOL_DENIED:
        print(_approval_text(f"    [denied] 已拒绝 {ev.tool_name}"), flush=True)
        # write_file/edit_file 拒绝后也关闭 VS Code diff
        if ev.tool_name in ("write_file", "edit_file"):
            _close_active_vscode_diff()
    elif kind == run_events.PLAN_CREATED:
        tasks = ev.payload.get("tasks", [])
        if len(tasks) > 1:  # 单任务计划不值得打面板,省噪音
            # 只打印计划摘要，不打整个任务面板，让第一个 spinner 负责打印
            # 这样避免了 PLAN_CREATED 后紧接着第一个任务开始时重复打印
            print(f"\n  ✻ 计划:{len(tasks)} 个子任务", flush=True)
            # 不更新 panel_state，让 spinner 的第一次 _commit_tasks 正常打印
    elif kind == run_events.RUN_COMPLETED:
        tasks = ev.payload.get("tasks", [])
        if len(tasks) > 1:
            task_lines = _format_task_lines(tasks)
            # 去重:与 spinner 内 _commit_tasks 共享 panel_state,签名相同则跳过
            if panel_state is not None:
                signature = "\n".join(task_lines)
                if signature == panel_state.get("last"):
                    return  # 已在 spinner 内打印过,不重复
                panel_state["last"] = signature
            print("  ✻ 完成:", flush=True)
            for line in task_lines:
                print(line, flush=True)
            print(flush=True)
    elif kind == run_events.RUN_FAILED:
        print(f"\n  ⚠ {ev.text}", flush=True)
        tasks = ev.payload.get("tasks", [])
        if len(tasks) > 1:
            task_lines = _format_task_lines(tasks)
            # 去重:与 spinner 内 _commit_tasks 共享 panel_state
            if panel_state is not None:
                signature = "\n".join(task_lines)
                if signature == panel_state.get("last"):
                    return  # 已打印过,不重复
                panel_state["last"] = signature
            for line in task_lines:
                print(line, flush=True)
        print(flush=True)
    elif kind == run_events.CONTEXT_BUDGET_WARNING:
        rep = ev.payload or {}
        pct = int(rep.get("usage_ratio", 0) * 100)
        print(f"  ⓘ 上下文已用 ~{pct}%(下个稳定点将压缩旧历史)", flush=True)
    elif kind == run_events.CONTEXT_COMPACTION_STARTED:
        print("  ✻ 压缩上下文 …", flush=True)
    elif kind == run_events.CONTEXT_COMPACTION_COMPLETED:
        p = ev.payload or {}
        before, after = p.get("token_before", 0), p.get("token_after", 0)
        print(f"  ✓ 已压缩旧历史(~{before} → ~{after} tokens)", flush=True)
    elif kind == run_events.CONTEXT_COMPACTION_FAILED:
        print(f"  ⚠ 上下文压缩跳过:{ev.text}", flush=True)
    # ── Loop Engineering 事件 ──
    elif kind == run_events.LOOP_STARTED:
        p = ev.payload or {}
        print(f"  ✻ Loop 开始: {ev.text}", flush=True)
        budgets = p.get("budgets", {})
        print(f"    预算: {budgets.get('max_iterations')}轮 / "
              f"{budgets.get('max_tool_calls')}次工具 / "
              f"{budgets.get('max_runtime_minutes')}分钟", flush=True)
    elif kind == run_events.LOOP_ITERATION_STARTED:
        print(f"  → Iteration {ev.payload.get('iteration')}", flush=True)
    elif kind == run_events.WORKTREE_PREPARED:
        print(f"  ✓ {ev.text}", flush=True)
    elif kind == run_events.VERIFICATION_STARTED:
        print(f"  ✻ {ev.text}", flush=True)
    elif kind == run_events.VERIFICATION_COMPLETED:
        p = ev.payload or {}
        status = p.get("status", "")
        emoji = "✓" if status == "pass" else "✗"
        print(f"  {emoji} {ev.text}", flush=True)
        checks = p.get("checks", [])
        if checks and status != "pass":
            for c in checks[:3]:  # 只打前 3 个,避免刷屏
                print(f"    · [{c.get('status')}] {c.get('name')}: {c.get('summary', '')[:80]}", flush=True)
    elif kind == run_events.REPAIR_PLANNED:
        print(f"  ⚙ {ev.text}", flush=True)
    elif kind == run_events.LOOP_COMPLETED:
        print(f"\n  ✓ {ev.text}", flush=True)
        p = ev.payload or {}
        if p.get("diff_summary"):
            print(f"    改动: {p['diff_summary'][:200]}", flush=True)
    elif kind == run_events.LOOP_FAILED:
        print(f"\n  ✗ {ev.text}", flush=True)
    elif kind == run_events.LOOP_BLOCKED:
        print(f"\n  ⊘ {ev.text}", flush=True)
    elif kind == run_events.LOOP_BUDGET_EXHAUSTED:
        print(f"\n  ⏱ {ev.text}", flush=True)


def _handle_context_command(session: AgentSession, line: str) -> str:
    """处理 /context 命令族(§7.3 的 CLI 入口)。

      /context                      显示预算 + recent window + summary 状态
      /context compact              立即压缩当前会话的可压缩历史
      /context summary              查看当前生效的压缩摘要
      /context disable-auto-compact 禁用本会话自动压缩
      /context enable-auto-compact  重新启用本会话自动压缩
    """
    ctx = getattr(session, "context_manager", None)
    if ctx is None:
        return "本会话未启用上下文预算(legacy 路径无压缩)。"
    parts = line.split(maxsplit=1)
    sub = parts[1].strip() if len(parts) > 1 else ""

    if sub == "":
        rep = ctx.report(session.messages, system=session.system_prompt)
        pct = int(rep["usage_ratio"] * 100)
        lines = [
            f"模型窗口   : {rep['model_context_limit']} tokens"
            f"(预留输出 {rep['reserved_output_tokens']})",
            f"预计输入   : ~{rep['estimated_input_tokens']} tokens(~{pct}%) "
            f"[{rep['status']}]",
            f"阈值       : 警告 {rep['warn_threshold']} / 强制压缩 {rep['compact_threshold']}",
            f"消息条数   : {rep['messages']}(压缩时至少保留最近 {rep['keep_recent']} 条)",
            f"自动压缩   : {'开' if rep['auto_compact'] else '关'}",
            f"已生成摘要 : {rep['summaries']} 段"
            f"{'(有生效摘要)' if rep['has_summary'] else ''}",
        ]
        return "\n".join(lines)

    if sub == "compact":
        compacted = ctx.maybe_compact(
            session.messages, system=session.system_prompt, force=True,
        )
        if compacted:
            return "已手动压缩当前会话的可压缩历史。"
        return "未压缩(没有可安全压缩的历史,或摘要生成失败)。"

    if sub == "summary":
        s = ctx.last_summary
        if s is None:
            return "当前没有生效的压缩摘要。历史还短或尚未触发压缩。"
        rng = s.source_message_range
        return (
            f"摘要范围   : 原始消息 [{rng[0]}, {rng[1]})  "
            f"压缩模型 {s.compression_model_profile or '(默认)'}\n"
            f"token      : 压缩前 ~{s.token_count_before} → 摘要 ~{s.token_count_after}\n"
            f"───\n{s.to_message_text()}"
        )

    if sub == "disable-auto-compact":
        ctx.auto_compact = False
        return "已禁用本会话自动压缩。仍可用 /context compact 手动压缩。"

    if sub == "enable-auto-compact":
        ctx.auto_compact = True
        return "已启用本会话自动压缩。"

    return ("未知子命令: {0}。可用: compact / summary / "
            "disable-auto-compact / enable-auto-compact").format(sub)


def _handle_model_command(router: SessionRouter, line: str) -> str:
    """处理 /model 命令族。

      /model                  列出所有配置的模型
      /model list             列出所有配置的模型
      /model current          显示当前使用的模型详情
      /model switch <profile> 切换到指定模型
    """
    from app.config.loader import load_profiles

    parts = line.split(maxsplit=2)
    sub = parts[1].strip() if len(parts) > 1 else "list"

    # /model 或 /model list - 列出所有配置的模型
    if sub == "" or sub == "list":
        try:
            profiles = load_profiles()
            if not profiles:
                return "未找到任何模型配置。请检查 config/models.yaml。"

            # 获取当前激活的 profile
            current_profile = None
            if router.current and hasattr(router.current, 'llm'):
                llm = router.current.llm
                # 尝试从 llm 对象获取 profile_name
                current_profile = getattr(llm, 'profile_name', None)
                # 如果 llm 没有 profile_name，尝试从配置获取
                if not current_profile:
                    from app.config.loader import load_config
                    try:
                        cfg = load_config()
                        current_profile = cfg.profile_name
                    except Exception:
                        pass

            lines = ["可用模型配置:"]
            lines.append("")

            for name, profile in sorted(profiles.items()):
                marker = "→ " if name == current_profile else "  "
                caps = ", ".join(profile.capabilities) if profile.capabilities else "无"
                lines.append(f"{marker}{name}")
                lines.append(f"    provider: {profile.provider}")
                lines.append(f"    model   : {profile.model}")
                if profile.base_url:
                    lines.append(f"    base_url: {profile.base_url}")
                lines.append(f"    能力    : {caps}")
                lines.append("")

            if current_profile:
                lines.append(f"当前使用: {current_profile} (标记为 →)")
            else:
                lines.append("当前未激活任何模型")

            return "\n".join(lines)
        except Exception as e:
            return f"加载模型配置失败: {e}"

    # /model current - 显示当前模型详情
    elif sub == "current":
        if not router.current:
            return "当前无活跃 session。用 /session new 创建。"

        session = router.current
        if not hasattr(session, 'llm'):
            return "当前 session 没有 LLM 配置。"

        llm = session.llm
        lines = ["当前模型配置:"]
        lines.append("")
        lines.append(f"  profile    : {getattr(llm, 'profile_name', '未知')}")
        lines.append(f"  provider   : {getattr(llm, 'provider', '未知')}")
        lines.append(f"  model      : {getattr(llm, 'model', '未知')}")

        base_url = getattr(llm, 'base_url', None)
        if base_url:
            lines.append(f"  base_url   : {base_url}")

        temperature = getattr(llm, 'temperature', None)
        if temperature is not None:
            lines.append(f"  temperature: {temperature}")

        reasoning_effort = getattr(llm, 'reasoning_effort', None)
        if reasoning_effort:
            lines.append(f"  reasoning  : {reasoning_effort}")

        context_size = getattr(llm, 'context_size', None)
        if context_size:
            lines.append(f"  context    : {context_size} tokens")

        # 显示使用统计
        if hasattr(session, 'cumulative_usage'):
            usage = session.cumulative_usage
            lines.append("")
            lines.append("本会话用量:")
            lines.append(f"  input      : {usage.get('input_tokens', 0)} tokens")
            lines.append(f"  output     : {usage.get('output_tokens', 0)} tokens")
            lines.append(f"  时长       : {session.cumulative_seconds:.1f}s")

        return "\n".join(lines)

    # /model switch <profile> - 切换模型
    elif sub == "switch":
        if len(parts) < 3:
            return "用法: /model switch <profile_name>\n使用 /model list 查看可用模型。"

        target_profile = parts[2].strip()

        try:
            profiles = load_profiles()
            if target_profile not in profiles:
                available = ", ".join(sorted(profiles.keys()))
                return f"未找到 profile '{target_profile}'。\n可用: {available}"

            # 切换模型需要重建 session
            # 这里我们通过设置环境变量并提示用户重启来实现
            # 因为动态切换模型涉及重建整个 LLM 实例和 session
            import os
            os.environ["ACTIVE_PROFILE"] = target_profile

            return (
                f"已设置 ACTIVE_PROFILE={target_profile}\n"
                f"\n"
                f"模型切换将在新建 session 时生效。建议:\n"
                f"  1. /session new      - 新建使用新模型的会话\n"
                f"  2. 或重启 agentlab   - 全局切换到新模型\n"
                f"\n"
                f"注: 当前会话仍使用原有模型。"
            )
        except Exception as e:
            return f"切换模型失败: {e}"

    else:
        return f"未知子命令: {sub}。可用: list / current / switch"


def _check_local_endpoint(cfg) -> None:
    """启动时探测本地 / 局域网 Ollama 端点是否在响应。

    背景:
      用户切到 local_qwen 这种 profile 时,如果 Ollama 没启动或没装,
      原先的报错要等到第一次工具调用 / chat 才出现,信息也是 SDK 内部
      的 ConnectionRefused,不友好。这里在 build_session 阶段提前 ping
      `/api/tags`,失败时给清晰的安装引导。

    只对"看起来是本地或局域网"的端点做检测(localhost / 127. / 192.168 /
    10. / 172.16-31),远程公网 OpenAI-compatible 端点不强检(可能临时网络
    抖动,等真实请求时让 SDK 自己报错更准)。
    """
    # 这些 provider 都走 OpenAICompatibleAdapter,需要本地 / 自建端点
    # (区别于 anthropic / openai 直连云端,无需检测)
    _LOCAL_PROVIDERS = {
        "openai_compatible", "ollama", "lmstudio", "vllm", "remote_openai_compatible",
    }
    if cfg.provider not in _LOCAL_PROVIDERS or not cfg.base_url:
        return

    base = cfg.base_url.lower()
    is_local = (
        "localhost" in base or "127.0.0.1" in base
        or "://192.168." in base or "://10." in base
        or "://172.1" in base or "://172.2" in base or "://172.3" in base
    )
    if not is_local:
        return

    # 把 /v1 之类的 OpenAI 后缀去掉,Ollama 健康检查接口是 /api/tags
    health_base = cfg.base_url.rstrip("/")
    if health_base.endswith("/v1"):
        health_base = health_base[:-3]
    health_url = f"{health_base}/api/tags"

    import urllib.error
    import urllib.request

    try:
        urllib.request.urlopen(health_url, timeout=3)
        return  # 服务正常
    except (urllib.error.URLError, OSError, ValueError):
        pass

    # 服务无响应:打印引导而不是让 SDK 报模糊的连接错误
    print(
        f"\n⚠ 无法连接到 {cfg.base_url}",
        file=sys.stderr,
    )
    print(
        "  本地 profile 需要 Ollama 服务在运行。可能原因:\n"
        "    1. 没装 Ollama        → 运行: bash scripts/install_local_model.sh\n"
        "                            或下载: https://ollama.com/download\n"
        "    2. 服务没启动         → 运行: ollama serve\n"
        "    3. 模型还没下载       → 运行: ollama pull qwen2.5-coder:7b-instruct\n"
        "    4. 局域网模式连不通   → 检查 5060Ti 主机防火墙 / OLLAMA_HOST=0.0.0.0\n\n"
        "  完整指南: docs/local_model_guide.md\n",
        file=sys.stderr,
    )
    sys.exit(2)


def _build_session(auto_approve: bool, profile: str | None) -> RuntimeService:
    cfg = load_config(profile_name=profile)
    if cfg.provider == "anthropic" and not (cfg.auth_token or cfg.api_key):
        print("未找到 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY。\n请在 .env 或 ~/.claude/settings.json 中配置后重试。", file=sys.stderr)
        sys.exit(2)

    # 本地 profile:先 ping Ollama,不通就提前给安装引导(而不是等首次工具调用才报错)
    _check_local_endpoint(cfg)

    llm = build_model_router(cfg)

    _print_init(f"== AgentLab v{__version__} ==")
    _print_init(f"provider : {cfg.provider}")
    _print_init(f"model    : {cfg.model}")
    if cfg.base_url:
        _print_init(f"base_url : {cfg.base_url}")
    if cfg.profile_name:
        _print_init(f"profile  : {cfg.profile_name}")
    if cfg.capabilities:
        _print_init(f"能力     : {', '.join(cfg.capabilities)}")
    from app.config.loader import workspace_root
    ws = workspace_root()
    _print_init(f"workspace: {ws}")

    # 动态生成工具列表
    from app.tools.builtin import default_tools
    builtin_tools = default_tools()
    tool_names = [t.name for t in builtin_tools]
    # 把 terminal_* 归纳显示
    tool_display = []
    for name in tool_names:
        if name.startswith('terminal_'):
            if 'terminal_* (交互式会话)' not in tool_display:
                tool_display.append('terminal_* (交互式会话)')
        else:
            tool_display.append(name)

    _print_init(f"工具     : {' / '.join(tool_display)}")
    _print_init("审批     : AUTO (-y)" if auto_approve else "审批     : 修改类工具会方向键菜单确认 (允许这次 / 总是允许 / 拒绝)")
    _print_init("输入 /version 查看版本; /image 或 Ctrl+V 附加图片; /reset 清空会话; /resume 继续未完成任务; /model [list|current|switch] 切换模型; /session [list|new|switch|...] 管理多 Agent; exit/quit 退出.")
    _print_init("执行中按 Esc 或 Ctrl-C 可中断,停下后直接输入新指令即可调整方向。\n")

    # ── MCP server 接入 ───────────────────────────────────────────────────────
    # 读 config/mcp_servers.yaml 中 enabled 的 server,启动 manager(供所有 session 共用)。
    # 没配过 / 没启用任何 server 时 mcp_manager 为 None,行为与之前一致。
    mcp_manager: MCPManager | None = None
    mcp_servers = enabled_servers()
    if mcp_servers:
        mcp_manager = MCPManager(mcp_servers)
        _print_init(f"MCP      : 正在连接 {len(mcp_servers)} 个 server "
                    f"({', '.join(s.name for s in mcp_servers)}) …", flush=True)
        mcp_manager.start()
        # 启用前展示:server、transport、发现的工具(落实 §9.2 "启用前展示可暴露能力")
        for s in mcp_servers:
            tool_names = [t.name for t in mcp_manager.tools() if t.server == s.name]
            _print_init(f"           ▸ {s.name} [{s.transport}] risk={s.risk} "
                        f"工具 {len(tool_names)} 个: {', '.join(tool_names) or '(无)'}")
        _print_init("           动作经外部 MCP server 执行;非只读工具默认每次需审批。")

        # ── 云端数据边界提示(PRD §7.8.2 / §12)─────────────────────────────────
        # 浏览器/桌面控制 + 云端模型时,页面快照/截图/DOM/表单内容会进 LLM 上下文
        # → 离开本机。带 named profile(--user-data-dir,而非默认 --isolated)的话,
        # 用户登录态下的真实数据更敏感。这条提示必须在首次观察之前给到用户。
        cloud_providers = {"anthropic", "openai"}
        browser_servers = [s for s in mcp_servers
                           if any(t.name.startswith("browser_") for t in mcp_manager.tools()
                                  if t.server == s.name)]
        if browser_servers and cfg.provider in cloud_providers:
            named = [s.name for s in browser_servers
                     if any("--user-data-dir" in str(a) for a in s.args)]
            print(
                f"⚠ 数据边界:浏览器 MCP({', '.join(s.name for s in browser_servers)})"
                f"启用,模型 provider 是云端 '{cfg.provider}'。\n"
                f"   页面截图 / DOM 摘要 / 表单内容会发送到云端模型用于推理。",
                file=sys.stderr,
            )
            if named:
                print(
                    f"   且 {', '.join(named)} 用 named persistent profile,"
                    f"登录态下访问的真实数据(邮件、文档、内部页面等)同样会进上下文。\n"
                    f"   只在你接受这种数据流向时使用。需要严格隔离请改回 isolated profile。",
                    file=sys.stderr,
                )

    # 启动校验:profile 声明了能力但没包含 "tools" → 警告(不阻断)
    if cfg.capabilities and "tools" not in cfg.capabilities:
        print(
            f"⚠ profile '{cfg.profile_name}' 没有声明 tools 能力,"
            f"模型可能不会调用工具或直接报错。\n"
            f"   如果确认模型支持工具,在 config/models.yaml 的 capabilities 加上 'tools'。\n",
            file=sys.stderr,
        )

    # ── Storage + SessionRouter ───────────────────────────────────────────────
    storage = Storage()
    approval_broker = ApprovalBroker()
    # CLI 继续使用原有同步菜单，但审批先进入 Broker；未来 HTTP/TUI 可不设置
    # fallback，通过 request_id 异步调用 RuntimeService.approve/deny。
    fallback_approval = AutoApprove() if auto_approve else InteractivePolicy()
    shared_approval = BrokerApprovalPolicy(approval_broker, fallback=fallback_approval)
    agent_profiles = load_agent_profiles()
    default_profile_id = cfg.profile_name or "default"

    # ── Skill Catalog ─────────────────────────────────────────────────────────
    # 扫描 skills/*/SKILL.md 生成 catalog（目录不存在则为空，行为与之前一致）。
    # Skill 只影响上下文（注入工作流说明），不授予工具权限。
    skill_catalog = SkillCatalog.from_dir()
    if skill_catalog.all():
        enabled = skill_catalog.enabled_skills()
        enabled_names = ', '.join(s.skill_id for s in enabled) if enabled else '无'
        _print_init(f"Skill    : 发现 {len(skill_catalog.all())} 个 "
                    f"({', '.join(s.skill_id for s in skill_catalog.all())});"
                    f" 默认启用 {len(enabled)} 个 ({enabled_names})")

    def _session_factory(agent_profile, session_id: str) -> AgentSession:
        """按 AgentProfile 构建一个隔离的 AgentSession:独立工具表 + 任务清单 + 记忆注入。"""
        task_store = TaskStore()

        def _persist_tool_audit(event) -> None:
            storage.log_tool_execution(
                session_id=session_id,
                tool_name=event.tool_name,
                args_summary=event.args_summary,
                result_summary=event.result_summary,
                is_error=event.is_error,
                elapsed_seconds=event.elapsed_seconds,
                risk=event.risk,
                target_type=event.target_type,
                scope=event.scope,
                origin=event.origin,
                host=event.host,
                approval_action=event.approval_action or "",
                outcome=event.outcome,
                requires_observation=event.requires_observation,
            )

        reg = ToolRegistry(audit_sink=_persist_tool_audit)
        for t in default_tools():
            reg.register(t)
        reg.register(make_todo_write_tool(task_store))
        # 交互式终端会话:每个 session 一个 PtySessionManager(子进程随会话生死),
        # 注册 terminal_open/send/close/list。cwd 锁 workspace,跟 shell 一致。
        pty_manager = PtySessionManager(cwd=str(ws))
        for t in make_terminal_tools(pty_manager):
            reg.register(t)
        # MCP 工具:所有 session 共用同一个 manager,但各自注册到自己的 registry
        if mcp_manager:
            mcp_tools = build_mcp_tools(mcp_manager, mcp_servers,
                                        reserved_names={t.name for t in reg.all()})
            for t in mcp_tools:
                reg.register(t)
        # Context Builder:按 memory_policy 把检索到的记忆注入 system prompt
        mem_policy = build_memory_policy(agent_profile.memory_policy, storage)
        recent = mem_policy.retrieve("", agent_profile.agent_id, limit=10)
        base_prompt = agent_profile.system_prompt or build_system_prompt(str(ws))
        # 先注入 Skill 工作流（按 AgentProfile.skills 显式启用），再注入记忆。
        # Skill 只加上下文，不放宽工具授权：上面 reg 注册的工具集才是实际可用集。
        with_skills = skill_catalog.inject(base_prompt, agent_profile.skills)
        sys_prompt = inject_memories(with_skills, recent)
        # ── 上下文预算 + 压缩(§7.3)──────────────────────────────────────────
        # 按当前模型窗口(profile 声明的 context_size 优先,否则按模型名查表)派生
        # 预算,在编排稳定点接近 85% 时自动压缩旧历史,避免长 run 撞 token 上限。
        ctx_budget = ContextBudget.from_model(
            model=cfg.model, declared_context_size=cfg.context_size,
        )
        ctx_manager = ContextManager(
            budget=ctx_budget,
            compressor=ContextCompressor(llm, model_profile=cfg.profile_name or ""),
            on_event=_print_run_event,
        )
        # ── progress 工厂 + panel_state(任务面板去重状态)────────────────────
        # _make_progress 返回 (progress_fn, panel_state),后者供 _print_run_event
        # 复用,避免 RUN_COMPLETED/RUN_FAILED 重复打印面板。
        progress_fn, panel_state = _make_progress(task_store)
        # 用 partial 把 panel_state 绑定到 _print_run_event,供 on_run_event 用
        from functools import partial
        print_run_event_with_state = partial(_print_run_event, panel_state=panel_state)
        sess = AgentSession(
            llm=llm,
            tools=reg,
            approval=shared_approval,
            system_prompt=sys_prompt,
            max_steps=agent_profile.max_steps,
            max_task_steps=agent_profile.max_task_steps,
            on_event=_print_event,
            progress=progress_fn,
            task_store=task_store,
            # PtySessionManager 随会话关闭(close_all 杀掉残留的交互式子进程)。
            # mcp_manager 不放这里:它由 router 全局统一关,否则切换/归档某个
            # session 会把全局 MCP 连接也关掉。
            closeables=[pty_manager],
            # ── 编排路径(§6.1)──────────────────────────────────────────────
            # 用 Planner/Executor/Replanner 编排:目标先拆任务,按依赖执行,失败
            # 重规划。Planner 用同一个 llm;system prompt 由 Orchestrator 注入到
            # 规划与执行两阶段。RunEvent 经 _print_run_event 渲染。
            orchestrate=True,
            planner=Planner(llm),
            on_run_event=print_run_event_with_state,
            context_manager=ctx_manager,
        )
        # 附加 agent_profile 供 CLI prompt 显示用(非 AgentSession 核心属性)
        sess.agent_profile = agent_profile
        sess.session_id = session_id
        # 附加 mem_policy 供退出时写摘要(§6.2,read_write 策略会话结束写入)
        sess.mem_policy = mem_policy
        return sess

    router = SessionRouter(
        storage=storage,
        session_factory=_session_factory,
        profiles=agent_profiles,
        default_profile_id=default_profile_id,
    )
    # 把 mcp_manager 挂到 router 上,main() 退出时统一关闭
    router.mcp_manager = mcp_manager

    # ── Loop Engineering 命令处理器 ──────────────────────────────────────────
    # 提供 /goal 和 /loop 命令。需要访问当前 session 的 Orchestrator、workspace_root、
    # 以及一个执行 loop.run() 的回调(带 Ctrl-C 取消处理)。
    from app.agent.loop_commands import LoopCommandHandler

    def _run_loop_with_cancel(loop) -> str:
        """执行 loop.run(),带 Ctrl-C 协作式取消(复用 _chat_with_cancel 的模式)。"""
        from app.agent.cancel import CancelToken
        import signal
        import threading

        token = CancelToken()
        can_trap = threading.current_thread() is threading.main_thread()
        prev_handler = None

        def _on_sigint(signum, frame):
            if not token.cancelled:
                token.cancel()
                print(f"\n  ⏹ 已请求中断;将在当前步骤后停止。", flush=True)
            else:
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                raise KeyboardInterrupt

        if can_trap:
            try:
                prev_handler = signal.signal(signal.SIGINT, _on_sigint)
            except ValueError:
                can_trap = False

        try:
            return loop.run(cancel=token)
        finally:
            if can_trap and prev_handler is not None:
                signal.signal(signal.SIGINT, prev_handler)

    router.loop_handler = LoopCommandHandler(
        storage=storage,
        get_session=lambda: router.current,
        run_loop_fn=_run_loop_with_cancel,
        workspace_root=ws,
    )

    service = RuntimeService(router, approval_broker=approval_broker)
    # 启动:有未归档历史 session 就恢复最近一个,否则新建(避免每次启动堆积空会话)
    start_agent = default_profile_id if default_profile_id in agent_profiles else None
    sid, resumed = service.resume_or_new(agent_id=start_agent)
    row = storage.get_session(sid)
    title = row["title"] if row else ""
    if resumed:
        msg_count = len(service.current.messages) if service.current else 0
        _print_init(f"会话     : 恢复 {sid}  ({title})  历史消息 {msg_count} 条 "
                    f"— /session new 开新会话, /session list 看全部\n")
    else:
        _print_init(f"会话     : 新建 {sid}  ({title})\n")
    return service


def _normalize_model_id(name: str | None) -> str:
    """规范化模型 ID 用于比较,消除分隔符 / 大小写差异。

    例:
      "claude-opus-4-6"   ↔ "claude-opus-4.6"   ↔ "Claude_Opus_4.6"
      规范化后都变成 "claude-opus-4-6",视为指向同一模型。

    避免代理用 `.` 而 AgentLab 用 `-` 这种纯命名风格差异每次都误报警告。
    真实的"模型映射"(claude-opus-4-9 → claude-3-5-sonnet-20241022)规范化后
    仍然不同,警告会照常出来。
    """
    if not name:
        return ""
    return name.lower().replace(".", "-").replace("_", "-")


def _print_stats(session: AgentSession) -> None:
    t, c = session.last_turn_usage, session.cumulative_usage
    print(
        f"  [stats] turn {session.last_turn_seconds:.1f}s "
        f"in={t['input_tokens']} out={t['output_tokens']} | "
        f"session {session.cumulative_seconds:.1f}s "
        f"in={c['input_tokens']} out={c['output_tokens']}",
        flush=True,
    )
    # 服务器实际返回的模型 ID 与请求模型不一致时给警告。
    # 这能立刻揭穿代理的"静默映射":你写 claude-opus-4-9 实际跑的是别的型号。
    # 规范化对比:忽略 . / - / _ / 大小写差异,这些只是命名风格而非模型本身不同。
    requested = session.llm.model
    actual = session.last_actual_model
    if actual and _normalize_model_id(actual) != _normalize_model_id(requested):
        print(
            f"  ⚠ requested '{requested}' but server returned model='{actual}'. "
            f"代理可能把请求的模型名映射到了别的真实模型。",
            file=sys.stderr,
        )


_PROMPT_STYLE = Style.from_dict({
    "prompt": "fg:#5fafff bold",
    "frame": "fg:#666666",
})


def _print_input_separator() -> None:
    """在每次提示输入前打一条灰色分隔线,把本次输入与上一轮回复区分开。"""
    if not sys.stdout.isatty():
        return
    width = max(20, _term_width() - 4)
    sys.stdout.write(f"\n\033[2;90m{'─' * width}\033[0m\n")
    sys.stdout.flush()


# 顶层斜杠命令 → 说明,用于补全菜单右侧 meta 文本
_SLASH_COMMANDS = {
    "/paste-image": "粘贴剪贴板图片并附加到下一条消息",
    "/image": "显示图片输入帮助；用 /image <路径> 添加图片",
    "/attachments": "查看或清空待发送图片",
    "/version": "显示当前 AgentLab 版本",
    "/reset": "清空当前会话的消息和任务",
    "/resume": "继续上一轮未完成/失败的任务",
    "/session": "管理多 Agent 会话",
    "/context": "查看上下文预算 / 手动压缩 / 摘要",
    "/goal": "定义 Loop Engineering 目标",
    "/loop": "启动/查看/停止 Loop",
    "/model": "查看和切换模型配置",
}
_ATTACHMENT_SUBCOMMANDS = {
    "clear": "清空尚未发送的图片",
    "clear-session": "清理当前 Session 的全部历史图片",
}
# /session 子命令 → 说明
_SESSION_SUBCOMMANDS = {
    "list": "列出所有活跃 session",
    "agents": "列出可用的 Agent",
    "new": "新建并切换到一个 Agent 会话",
    "switch": "切换到已有 session",
    "rename": "重命名当前 session",
    "archive": "归档当前 session(软删除,可恢复)",
    "delete": "彻底删除 session 及消息(不可恢复)",
}
# /context 子命令 → 说明
_CONTEXT_SUBCOMMANDS = {
    "compact": "立即压缩当前会话的可压缩历史",
    "summary": "查看当前生效的压缩摘要",
    "disable-auto-compact": "禁用本会话的自动压缩",
    "enable-auto-compact": "重新启用本会话的自动压缩",
}
# /goal 子命令 → 说明
_GOAL_SUBCOMMANDS = {
    "new": "创建新 GoalSpec",
    "show": "显示当前或指定 goal",
}
# /loop 子命令 → 说明
_LOOP_SUBCOMMANDS = {
    "start": "启动 Loop",
    "status": "显示 Loop 状态",
    "stop": "停止 Loop (Ctrl-C)",
}
# /model 子命令 → 说明
_MODEL_SUBCOMMANDS = {
    "list": "列出所有配置的模型",
    "current": "显示当前使用的模型详情",
    "switch": "切换到指定模型",
}


class _SlashCompleter(Completer):
    """斜杠命令补全器:输入 `/` 时弹出命令,并对 /session 做子命令 / 参数补全。

    三级补全:
      1. `/` / `/se…`           → 顶层命令(/reset, /session)
      2. `/session ` + 子命令前缀 → list/agents/new/switch/rename/archive
      3. `/session switch ` + 前缀 → 已有 session id(从 router 实时取)
         `/session new ` + 前缀    → 可用 agent id
    普通文本(不以 / 开头)不补全,不打扰正常对话输入。
    """

    def __init__(self, router: SessionRouter):
        self._router = router

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return  # 普通对话输入,不补全

        parts = text.split()
        # ── 第一级:顶层命令 ──（还没打空格,或就一个 token）
        if len(parts) <= 1 and not text.endswith(" "):
            word = parts[0] if parts else "/"
            for cmd, desc in _SLASH_COMMANDS.items():
                if cmd.startswith(word):
                    yield Completion(cmd, start_position=-len(word), display_meta=desc)
            return

        if parts[0] not in ("/session", "/context", "/goal", "/loop", "/model", "/attachments"):
            return  # 只有这几个有更深层补全

        # ── 第二级:/attachments 子命令 ──
        if parts[0] == "/attachments":
            if len(parts) == 1 or (len(parts) == 2 and not text.endswith(" ")):
                prefix = parts[1] if len(parts) == 2 else ""
                for sub, desc in _ATTACHMENT_SUBCOMMANDS.items():
                    if sub.startswith(prefix):
                        yield Completion(
                            sub, start_position=-len(prefix), display_meta=desc,
                        )
            return

        # ── 第二级:/context 子命令 ──
        if parts[0] == "/context":
            if len(parts) == 1 or (len(parts) == 2 and not text.endswith(" ")):
                prefix = parts[1] if len(parts) == 2 else ""
                for sub, desc in _CONTEXT_SUBCOMMANDS.items():
                    if sub.startswith(prefix):
                        yield Completion(sub, start_position=-len(prefix), display_meta=desc)
            return

        # ── 第二级:/goal 子命令 ──
        if parts[0] == "/goal":
            if len(parts) == 1 or (len(parts) == 2 and not text.endswith(" ")):
                prefix = parts[1] if len(parts) == 2 else ""
                for sub, desc in _GOAL_SUBCOMMANDS.items():
                    if sub.startswith(prefix):
                        yield Completion(sub, start_position=-len(prefix), display_meta=desc)
            return

        # ── 第二级:/loop 子命令 ──
        if parts[0] == "/loop":
            if len(parts) == 1 or (len(parts) == 2 and not text.endswith(" ")):
                prefix = parts[1] if len(parts) == 2 else ""
                for sub, desc in _LOOP_SUBCOMMANDS.items():
                    if sub.startswith(prefix):
                        yield Completion(sub, start_position=-len(prefix), display_meta=desc)
            return

        # ── 第二级:/model 子命令 ──
        if parts[0] == "/model":
            if len(parts) == 1 or (len(parts) == 2 and not text.endswith(" ")):
                prefix = parts[1] if len(parts) == 2 else ""
                for sub, desc in _MODEL_SUBCOMMANDS.items():
                    if sub.startswith(prefix):
                        yield Completion(sub, start_position=-len(prefix), display_meta=desc)
            # ── 第三级:/model switch <profile_name> 的参数 ──
            elif len(parts) >= 2 and parts[1] == "switch":
                from app.config.loader import load_profiles
                arg_prefix = parts[2] if (len(parts) >= 3 and not text.endswith(" ")) else ""
                try:
                    profiles = load_profiles()
                    for profile_name, profile in profiles.items():
                        if profile_name.startswith(arg_prefix):
                            desc = f"{profile.provider} / {profile.model}"
                            yield Completion(profile_name, start_position=-len(arg_prefix),
                                           display_meta=desc)
                except Exception:
                    pass
            return

        # ── 第二级:/session 子命令 ──
        if len(parts) == 1 or (len(parts) == 2 and not text.endswith(" ")):
            prefix = parts[1] if len(parts) == 2 else ""
            for sub, desc in _SESSION_SUBCOMMANDS.items():
                if sub.startswith(prefix):
                    yield Completion(sub, start_position=-len(prefix), display_meta=desc)
            return

        # ── 第三级:switch/delete <session_id> / new <agent_id> 的参数 ──
        sub = parts[1]
        arg_prefix = parts[2] if (len(parts) >= 3 and not text.endswith(" ")) else ""
        if sub in ("switch", "delete"):
            for row in self._router.list_sessions():
                if row["id"].startswith(arg_prefix):
                    yield Completion(row["id"], start_position=-len(arg_prefix),
                                     display_meta=row.get("title", ""))
        elif sub == "new":
            for aid, prof in self._router.list_profiles().items():
                if aid.startswith(arg_prefix):
                    yield Completion(aid, start_position=-len(arg_prefix),
                                     display_meta=getattr(prof, "name", ""))



def _scan_for_esc(data: bytes) -> bool:
    """判断一段从 stdin 读到的字节里是否含"单独的 Esc 键"。

    Esc 键本身是单字节 0x1b。但方向键 / 功能键等也以 0x1b 开头(转义序列,如
    方向键是 ESC [ A)。区分办法:0x1b 后面紧跟 '[' 或 'O' 的是转义序列,不算;
    0x1b 是这段数据的最后一个字节、或后面跟的不是 '[' / 'O',才认定为真正的 Esc。

    抽成纯函数便于单测(终端 raw mode 本身难自动化测)。
    """
    i = 0
    n = len(data)
    while i < n:
        if data[i] == 0x1B:  # ESC
            nxt = data[i + 1] if i + 1 < n else None
            if nxt in (0x5B, 0x4F):  # '[' or 'O' → 转义序列(方向键等),跳过整段
                i += 2
                continue
            return True  # 单独的 Esc
        i += 1
    return False


class _EscWatcher:
    """监听 stdin 的 Esc 键,按下时调 on_esc()。用作上下文管理器。

    为什么需要它:模型调用是同步阻塞的,普通按键(非信号)无法像 Ctrl-C 那样靠
    signal 打断。这里把终端切到 cbreak(非规范、无回显),起一个 daemon 线程轮询
    stdin;读到单独的 Esc 就回调(通常去置位 CancelToken),实现"按 Esc 中断当前
    回复、回到提示符重新引导"。

    优雅降级:非 TTY(管道/重定向)或 Windows(无 termios)时不启用,直接空转,
    用户仍可用 Ctrl-C。退出时务必恢复终端原状(finally)。
    """

    def __init__(self, on_esc):
        self._on_esc = on_esc
        self._stop = threading.Event()
        self._paused = threading.Event()  # 置位时后台线程不读 stdin(让前台菜单独占)
        self._thread = None
        self._fd = None
        self._saved = None
        self._enabled = self._can_enable()

    def pause(self) -> None:
        """暂停读 stdin(前台 prompt_toolkit 菜单需要独占时调,见 input_arbiter)。"""
        self._paused.set()

    def resume(self) -> None:
        """恢复读 stdin。"""
        self._paused.clear()

    @staticmethod
    def _can_enable() -> bool:
        if platform.system() == "Windows":
            return False
        try:
            return sys.stdin.isatty()
        except (ValueError, AttributeError):
            return False

    def __enter__(self) -> "_EscWatcher":
        if not self._enabled:
            return self
        import termios
        import tty
        try:
            self._fd = sys.stdin.fileno()
            self._saved = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)  # 非规范模式:按键即可读,无需回车;保留 Ctrl-C 信号
        except (termios.error, ValueError, OSError):
            self._enabled = False  # 拿不到终端属性就放弃,降级到仅 Ctrl-C
            return self
        # 注册为当前后台 reader,供菜单等前台 UI 临时暂停(独占 stdin)
        from app.util.input_arbiter import set_background_reader
        set_background_reader(self)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        if self._enabled:
            from app.util.input_arbiter import clear_background_reader
            clear_background_reader(self)
        if self._enabled and self._saved is not None and self._fd is not None:
            import termios
            try:
                termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            except (termios.error, ValueError, OSError):
                pass

    def _run(self) -> None:
        import select
        fired = False
        while not self._stop.is_set():
            if self._paused.is_set():
                # 暂停期间不碰 stdin(前台菜单在用),睡一小会儿再看
                time.sleep(0.05)
                continue
            try:
                r, _, _ = select.select([self._fd], [], [], 0.1)
            except (ValueError, OSError):
                break  # fd 被关闭(主线程在退出),停
            if not r or self._paused.is_set():
                continue
            try:
                data = os.read(self._fd, 64)
            except (OSError, ValueError):
                break
            if not data:
                continue
            if not fired and _scan_for_esc(data):
                fired = True  # 只触发一次,避免连发
                try:
                    self._on_esc()
                except Exception:
                    pass


def _chat_with_cancel(
    session: AgentSession,
    line: str,
    *,
    resume: bool = False,
    service: RuntimeService | None = None,
    images: list[ImageAttachment] | None = None,
) -> str:
    """跑一轮 chat,并把 Ctrl-C 接到协作式取消(对应 PRD 紧急停止)。

    编排路径(orchestrate=True)的模型调用是同步阻塞的,无法被强行打断;取消采用
    协作式:Ctrl-C 时把 CancelToken 置位,Orchestrator 在下一个安全检查点(claim
    下一个任务前 / 调模型前 / 执行工具前)抛 Cancelled 干净退出。第一次 Ctrl-C 触发
    取消;若用户连按(取消尚未生效),让默认 KeyboardInterrupt 冒泡到上层中断。

    resume=True 时继续上一轮未完成的任务(失败任务重置为 pending)。

    取消有两个入口,都置位同一个 CancelToken:
      - Ctrl-C:signal 处理器(连按强制中断);
      - Esc:_EscWatcher 监听 stdin(按一下中断,回提示符后可重新输入引导方向)。
    中断后历史保持可继续状态(executor 会给未执行工具补合成结果),下一轮输入即可
    在被中断的上下文上调整方向(steering)。

    只在主线程、且 stdin 为 TTY 时装 SIGINT 处理器;非交互(单测 / 管道)直接跑。
    """
    import signal

    token = CancelToken()
    can_trap = threading.current_thread() is threading.main_thread()
    prev_handler = None

    def _request_cancel(via: str) -> None:
        if not token.cancelled:
            token.cancel()
            print(f"\n  ⏹ 已请求中断({via});将在当前步骤后停止。"
                  f"停下后可直接输入新指令调整方向,或 /resume 继续。", flush=True)

    if can_trap:
        def _on_sigint(signum, frame):
            if not token.cancelled:
                _request_cancel("Ctrl-C")
            else:
                # 已请求过取消仍按:恢复默认行为,抛 KeyboardInterrupt 强制中断
                signal.signal(signal.SIGINT, signal.SIG_DFL)
                raise KeyboardInterrupt
        try:
            prev_handler = signal.signal(signal.SIGINT, _on_sigint)
        except ValueError:
            can_trap = False  # 不在主线程,signal 不可用

    try:
        with _EscWatcher(on_esc=lambda: _request_cancel("Esc")):
            if service is not None:
                return service.send_message(
                    line, images=images, cancel=token, resume=resume,
                )
            # 兼容直接传 AgentSession 的旧测试和第三方调用方。
            return session.chat(
                line, images=images, cancel=token, resume=resume,
            )
    finally:
        if can_trap and prev_handler is not None:
            signal.signal(signal.SIGINT, prev_handler)


def _image_command(line: str) -> tuple[str, str | None]:
    """解析 /image；返回 (action, payload)，裸命令只显示帮助，绝不发给 Agent。"""
    if line == "/image":
        return "help", None
    if line.startswith("/image "):
        value = line[len("/image "):].strip()
        return ("path", value) if value else ("help", None)
    return "not_image", None


def _image_help_text() -> str:
    return (
        "图片输入方式:\n"
        "  1. 复制图片后按 Ctrl+V / Shift+Insert，再输入问题并回车\n"
        "  2. /paste-image 读取系统剪贴板图片\n"
        "  3. /image <图片路径> 仅加入待发送队列\n"
        "  4. /image <图片路径> -- <提示词> 立即发送图片和提示词\n"
        "输入 /attachments 可查看待发送图片。"
    )


_IMAGE_PLACEHOLDER_RE = re.compile(r"\[AgentLab:image:([A-Za-z0-9_-]+)\]")


def _clipboard_key_bindings(
    pending_clipboard: dict[str, object],
    attachment_store: AttachmentStore,
    get_session_id,
) -> KeyBindings:
    """Ctrl+V/Shift+Insert：剪贴板是图片时加入待发送队列，否则正常粘贴文本。"""
    bindings = KeyBindings()

    def _paste(event) -> None:
        clipboard = capture_system_clipboard()
        if clipboard.image is not None:
            try:
                image = attachment_store.add_bytes(
                    get_session_id() or "unbound",
                    clipboard.image.data,
                    clipboard.image.name,
                )
            except AttachmentError as exc:
                pending_clipboard["error"] = str(exc)
                return
            images = pending_clipboard.setdefault("images", {})
            images[image.attachment_id] = image
            event.current_buffer.insert_text(
                f"[AgentLab:image:{image.attachment_id}]"
            )
            get_app().invalidate()
            return
        # 非图片剪贴板必须保留 prompt_toolkit 原有的文本粘贴体验。
        if clipboard.text:
            event.current_buffer.insert_text(clipboard.text)

    bindings.add("c-v")(_paste)
    bindings.add("s-insert")(_paste)
    return bindings


def _extract_pasted_images(
    line: str,
    pending_clipboard: dict[str, object],
) -> tuple[str, list[ImageAttachment]]:
    images_by_id = pending_clipboard.get("images", {})
    selected: list[ImageAttachment] = []

    def replace(match: re.Match) -> str:
        attachment = images_by_id.get(match.group(1))
        if attachment is not None and attachment not in selected:
            selected.append(attachment)
        return ""

    text = _IMAGE_PLACEHOLDER_RE.sub(replace, line).strip()
    # /paste-image 和 /image（无内联 prompt）没有占位符，也约定附加到下一条
    # 普通消息，因此把队列中剩余图片一并发送。发送后才清空，slash 命令不会误吞。
    for attachment in list(images_by_id.values()):
        if attachment not in selected:
            selected.append(attachment)
    images_by_id.clear()
    return text, selected


def _format_pending_attachments(pending_clipboard: dict[str, object]) -> str:
    images = list((pending_clipboard.get("images") or {}).values())
    if not images:
        return "当前没有待发送图片。"
    lines = [f"待发送图片: {len(images)} 张"]
    for image in images:
        lines.append(
            f"  - {image.attachment_id}: {image.original_name} "
            f"({image.width}x{image.height}, {image.size_bytes} bytes)"
        )
    return "\n".join(lines)


def _repl(router: RuntimeService) -> int:
    """交互式对话。

    用 prompt_toolkit 替代内建 input(),解决:
      - 中文宽字符按退格只删 1 列、视觉残留的问题
      - 缺少历史回放(↑/↓)、Ctrl-A/E 编辑等
    Ctrl-C 清空当前行(不退出);Ctrl-D / exit / quit 退出。
    斜杠命令:/reset 清空当前会话;/session ... 管理多 Agent。
    输入 `/` 时弹出命令补全(由 _SlashCompleter 提供)。
    """
    attachment_store = AttachmentStore()
    pending_clipboard: dict[str, object] = {"images": {}}
    pt_session: PromptSession = PromptSession(
        history=InMemoryHistory(),
        completer=_SlashCompleter(router),
        complete_while_typing=True,   # 边打边弹,不用按 Tab
        key_bindings=_clipboard_key_bindings(
            pending_clipboard,
            attachment_store,
            lambda: router.current_id,
        ),
    )

    while True:
        _print_input_separator()
        # 动态构建 prompt:显示 [session_id·标题]
        if router.current_id and router.current:
            sess_id = router.current_id
            # 从 router._storage 获取 session 标题
            sess_row = router._storage.get_session(sess_id)
            title = sess_row["title"] if sess_row else "?"
            # 标题可能很长，截断到合理长度
            if len(title) > 30:
                title = title[:27] + "..."
            prompt_text = f"[{sess_id[:6]}·{title}] ▸ "
        else:
            prompt_text = "▸ "
        prompt_fragments = FormattedText([("class:prompt", prompt_text)])

        try:
            line = pt_session.prompt(prompt_fragments, style=_PROMPT_STYLE).strip()
        except KeyboardInterrupt:
            # Ctrl-C: 清空当前行后继续
            continue
        except EOFError:
            # Ctrl-D: 退出
            print()
            return 0

        if not line:
            # Ctrl+V 粘贴图片会插入可见占位符，因此这里不会误丢图片。
            continue
        if line in ("exit", "quit"):
            return 0
        if line == "/version":
            print(version_text())
            continue
        image_action, image_payload = _image_command(line)
        if image_action == "help":
            print(_image_help_text())
            continue
        if line == "/paste-image":
            clipboard = capture_system_clipboard()
            if clipboard.image is None:
                print("剪贴板中没有图片。")
                continue
            try:
                current_session = router.current
                if current_session is None or "vision" not in getattr(
                    current_session.llm, "capabilities", [],
                ):
                    print(
                        "当前模型 profile 未声明 vision 能力，不能粘贴图片；"
                        "请先切换到视觉模型。"
                    )
                    continue
                image = attachment_store.add_bytes(
                    router.current_id or "unbound",
                    clipboard.image.data,
                    clipboard.image.name,
                )
            except AttachmentError as exc:
                print(f"无法粘贴图片: {exc}")
                continue
            pending_clipboard["images"][image.attachment_id] = image
            print(
                f"已附加剪贴板图片 {image.attachment_id} "
                f"({image.width}x{image.height})；下一条消息将发送该图片。"
            )
            continue
        if line == "/attachments":
            print(_format_pending_attachments(pending_clipboard))
            continue
        if line == "/attachments clear-session":
            session = router.current
            if session is None or router.current_id is None:
                print("当前无活跃 session。")
                continue
            try:
                approved = session.approval.request(
                    "clear_session_images",
                    {
                        "session_id": router.current_id,
                        "path": str(attachment_store.root / router.current_id),
                        "purpose": "delete_all_session_images",
                    },
                )
            except Exception as exc:
                print(f"图片清理审批失败: {format_exception(exc)}")
                continue
            if not approved:
                print("已取消清理 Session 图片。")
                continue
            try:
                result = router.clear_session_images()
            except Exception as exc:
                print(f"清理 Session 图片失败: {format_exception(exc)}")
                continue
            pending_clipboard["images"].clear()
            print(
                f"已清理当前 Session 的全部图片：删除 {result['files']} 个文件，"
                f"移除 {result['references']} 个历史引用。文本对话已保留。"
            )
            continue
        if line == "/attachments clear":
            pending_clipboard["images"].clear()
            print("已清空待发送图片。")
            continue

        # 图片附件必须进入当前 session 的隔离目录；切换 session 时清空待发送队列，
        # 避免上一会话的敏感截图被误发到另一个 Agent。
        if line.startswith("/session"):
            before_session = router.current_id
            out = router.handle_session_command(line)
            if router.current_id != before_session:
                pending_clipboard["images"].clear()
            if out is not None:
                print(out)
            continue

        session = router.current
        if session is None:
            print("当前无活跃 session。用 /session new 创建。")
            continue

        direct_images: list[ImageAttachment] = []
        if image_action == "path":
            raw = image_payload or ""
            # `--` 是唯一“立即发送”分隔符；只有路径时永远只入队，不触发 Agent。
            path_text, separator, prompt = raw.partition(" -- ")
            if not path_text:
                print("用法: /image <图片路径> [-- 提示词]")
                continue
            try:
                image = attachment_store.add_path(
                    router.current_id or "unbound",
                    path_text.strip('"\''),
                    workspace_root=workspace_root(),
                    approval=getattr(session, "approval", None),
                )
            except (AttachmentError, OSError) as exc:
                print(f"无法附加图片: {exc}")
                continue
            if "vision" not in getattr(session.llm, "capabilities", []):
                print(
                    "当前模型 profile 未声明 vision 能力，不能发送图片；"
                    "请先用 /model switch 切换到视觉模型并新建 session。"
                )
                continue
            if separator and prompt.strip():
                line = prompt.strip()
                direct_images = [image]
            else:
                pending_clipboard["images"][image.attachment_id] = image
                print(
                    f"已附加 {image.original_name} ({image.width}x{image.height})；"
                    "下一条消息将发送该图片。"
                )
                continue
        else:
            direct_images = []

        # 其余 slash 命令也不应消耗待发送附件。
        if line == "/reset":
            session.reset()
            print("(history cleared)")
            continue

        if line == "/context" or line.startswith("/context "):
            print(_handle_context_command(session, line))
            continue

        # /model 命令:查看和切换模型配置
        if line == "/model" or line.startswith("/model "):
            print(_handle_model_command(router, line))
            continue

        # /goal 和 /loop 命令:Loop Engineering 模式(§7.6)
        if line.startswith("/goal") or line.startswith("/loop"):
            loop_handler = getattr(router, "loop_handler", None)
            if loop_handler is None:
                print("Loop 功能尚未初始化。")
                continue
            if line.startswith("/goal"):
                out = loop_handler.handle_goal_command(line)
            else:
                out = loop_handler.handle_loop_command(line)
            if out:
                print(out)
            continue

        line, pasted_images = _extract_pasted_images(line, pending_clipboard)
        images = direct_images + pasted_images
        if not line and images:
            line = "请分析这些图片。"

        # /resume:继续上一轮未完成的任务(失败任务重置为 pending 重试)。
        # 可带补充说明(/resume <提示>),否则用默认继续指令。
        if line == "/resume" or line.startswith("/resume "):
            store = getattr(session, "task_store", None)
            if store is None or store.is_empty():
                print("没有可继续的任务(任务列表为空)。直接输入新目标即可开始。")
                continue
            summary = store.summary()
            if not summary.get("failed") and not summary.get("pending") \
                    and not summary.get("blocked") and not summary.get("in_progress"):
                print("所有任务都已完成,没有需要继续的任务。")
                continue
            extra = line[len("/resume"):].strip()
            goal = extra or "继续完成上一轮未完成的任务。"
            try:
                _chat_with_cancel(
                    session,
                    goal,
                    resume=True,
                    service=router,
                    images=images,
                )
            except Exception as exc:
                print(f"  !! {format_exception(exc)}\n", file=sys.stderr)
                continue
            _print_stats(session)
            continue

        try:
            repaired = _repair_dangling_tool_use(session)
            if repaired:
                print(f"  ⚠ 检测到 {repaired} 个悬空 tool_use，已自动修复（历史损坏可能由上次中断引起）。",
                      file=sys.stderr)
            _chat_with_cancel(session, line, service=router, images=images)
        except Exception as exc:
            # 脱敏后输出,避免 Authorization / API key 等凭据泄漏到终端
            print(f"  !! {format_exception(exc)}\n", file=sys.stderr)
            continue
        _print_stats(session)


def _repair_dangling_tool_use(session) -> int:
    """检测并修复 messages 里悬空的 tool_use（没有配对 tool_result 的 assistant 消息）。

    Anthropic / Bedrock 要求每个 tool_use 块后面紧跟 tool_result，否则报
    TOOL_USE_RESULT_MISMATCH 500 错误。executor 的 except BaseException 兜底已
    能阻止新损坏产生；这个函数处理已存在于 session.messages 里的历史损坏
    （从 SQLite 恢复的旧会话、或极端情况下漏网的损坏）。

    返回修复的悬空 tool_use 数量（0 表示无需修复）。
    """
    from app.agent.executor import INTERRUPTED_TOOL_RESULT

    messages = getattr(session, "messages", None)
    if not messages:
        return 0

    llm = getattr(session, "llm", None)
    if llm is None:
        return 0

    repaired = 0
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") != "assistant":
            i += 1
            continue
        # 收集本条 assistant 消息里所有 tool_use 的 id
        content = msg.get("content", [])
        tool_use_ids = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use_ids.append(block["id"])
        if not tool_use_ids:
            i += 1
            continue
        # 检查紧跟的下一条消息是否是 tool_result
        next_i = i + 1
        if next_i < len(messages):
            next_msg = messages[next_i]
            next_content = next_msg.get("content", [])
            if isinstance(next_content, list):
                result_ids = {
                    b.get("tool_use_id") for b in next_content
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                }
                if result_ids >= set(tool_use_ids):
                    i += 1
                    continue  # 配对完整
        # 有悬空 tool_use，在其后插入合成 tool_result
        from app.models.protocol import ToolResult
        synthetic = [
            ToolResult(tool_call_id=tid, output=INTERRUPTED_TOOL_RESULT, is_error=False)
            for tid in tool_use_ids
        ]
        patch = llm.format_tool_results(synthetic)
        messages[next_i:next_i] = patch
        repaired += len(tool_use_ids)
        i += 2  # 跳过刚插入的 tool_result
    return repaired


def _workspace_directory(value: str) -> Path:
    """解析并校验 CLI workspace 参数。"""
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise argparse.ArgumentTypeError(f"workspace 不可访问: {exc}") from exc
    if not path.is_dir():
        raise argparse.ArgumentTypeError(f"workspace 不是目录: {path}")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentlab")
    parser.add_argument(
        "--version",
        action="version",
        version=version_text(),
        help="显示当前 AgentLab 版本后退出",
    )
    parser.add_argument("-p", "--prompt", help="一次性 prompt，执行完即退出")
    parser.add_argument(
        "--image", action="append", default=[],
        help="为单次 prompt 附加图片路径，可重复使用",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="自动放行所有工具调用")
    parser.add_argument("--profile", help="使用 config/models.yaml 中的指定 profile")
    parser.add_argument(
        "-w",
        "--workspace",
        type=_workspace_directory,
        help="Agent 可操作的工作区目录；相对路径按当前终端目录解析",
    )
    args = parser.parse_args(argv)
    if args.image and not args.prompt:
        parser.error("--image 必须和 -p/--prompt 一起使用；交互模式请用 /image")

    # 显式 CLI 参数优先于项目 .env。load_dotenv(override=False) 会保留该值。
    if args.workspace is not None:
        os.environ["WORKSPACE_ROOT"] = str(args.workspace)

    try:
        router = _build_session(auto_approve=args.yes, profile=args.profile)

        try:
            if args.prompt:
                session = router.current
                images: list[ImageAttachment] = []
                try:
                    attachment_store = AttachmentStore()
                    for image_path in args.image:
                        images.append(attachment_store.add_path(
                            router.current_id or "unbound",
                            image_path,
                            workspace_root=workspace_root(),
                            approval=session.approval,
                        ))
                    _chat_with_cancel(
                        session, args.prompt, service=router, images=images,
                    )
                finally:
                    _print_stats(session)
                return 0
            return _repl(router)
        finally:
            router.close()  # 取消 run/审批并关闭所有 session、PTY 和 MCP
    except KeyboardInterrupt:
        print()
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        # 顶层兜底:打印脱敏后的 traceback,避免凭据泄漏
        sys.stderr.write(format_traceback(exc))
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
