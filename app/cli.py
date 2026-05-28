"""AgentLab CLI —— 程序入口和用户界面。

用法:
    python -m app                              # 交互式对话
    python -m app -p "帮我看 README.md"        # 单次 prompt 后退出
    python -m app -y                           # 自动放行所有工具调用
    python -m app --profile cloud_claude       # 使用指定 profile

退出: exit / quit / Ctrl-D
中断当前输入: Ctrl-C
重置会话: /reset
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager

from prompt_toolkit import PromptSession
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.styles import Style

from app.agent.approval import AutoApprove, InteractivePolicy
from app.agent.runtime import AgentSession, TurnEvent
from app.config.loader import load_config
from app.models.router import build_model_router
from app.tools.builtin.files import default_tools
from app.tools.registry import ToolRegistry
from app.util.redact import format_exception, format_traceback

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _term_width(default: int = 80) -> int:
    try:
        cols = shutil.get_terminal_size((default, 24)).columns
    except (OSError, ValueError):
        return default
    # 某些 PTY (例如 script 命令) 会返回 0,需要兜底为合理默认
    return cols if cols > 0 else default


def _display_width(text: str) -> int:
    """终端显示宽度：东亚宽字符算 2，其他算 1。"""
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


class _Spinner:
    """thinking 状态行 + 实时 token 计数 + 流式文本输出。

    布局：流式文本在上，spinner 行始终钉在底部。每次 token 更新或文本到达，
    都用 ANSI 重绘 text + spinner 区域，保持 spinner 在最下面持续刷新。

    向 Runtime 暴露两个回调入口：
      update(metrics)  - token 进度更新
      on_text(delta)   - 文本增量；累积到 text_buffer，立即重绘
    """

    def __init__(self, label: str):
        self.label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._t0 = 0.0
        self._lock = threading.Lock()
        self._metrics: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._text_buffer = ""
        self._frame_idx = 0
        self._last_drawn_lines = 0  # 上次写入占用的屏幕行数（含 spinner 行）

    def __enter__(self) -> "_Spinner":
        self._t0 = time.monotonic()
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        with self._lock:
            # 最终态：清掉 spinner 行，只保留正文（runtime 后续打 [stats]）
            self._erase_last()
            if self._text_buffer:
                sys.stdout.write(self._text_buffer)
                if not self._text_buffer.endswith("\n"):
                    sys.stdout.write("\n")
                sys.stdout.write("\n")
            else:
                # 没有文本（纯 tool_use），保留一行 thinking 摘要
                sys.stdout.write(f"  ✻ {self.label} ({self._fmt_status()})\n")
            sys.stdout.flush()

    def update(self, metrics: dict[str, int]) -> None:
        with self._lock:
            self._metrics = {
                "input_tokens": int(metrics.get("input_tokens", 0) or 0),
                "output_tokens": int(metrics.get("output_tokens", 0) or 0),
            }
            self._redraw()

    def on_text(self, delta: str) -> None:
        if not delta:
            return
        with self._lock:
            self._text_buffer += delta
            self._redraw()

    def _fmt_status(self) -> str:
        elapsed = time.monotonic() - self._t0
        out_t = self._metrics["output_tokens"]
        if out_t > 0:
            return f"{_fmt_duration(elapsed)} · ↓ {_fmt_tokens(out_t)}"
        return _fmt_duration(elapsed)

    def _erase_last(self) -> None:
        """擦掉上一次绘制的内容。光标在最后一行末尾，需要回到起点并清屏到末尾。"""
        if self._last_drawn_lines <= 0:
            return
        sys.stdout.write("\r")
        if self._last_drawn_lines > 1:
            sys.stdout.write(f"\033[{self._last_drawn_lines - 1}A")
        sys.stdout.write("\033[J")
        self._last_drawn_lines = 0

    def _redraw(self) -> None:
        """擦旧 → 写 text → 写 spinner。调用方需持有 self._lock。"""
        self._erase_last()

        term_width = _term_width()

        text_lines = 0
        if self._text_buffer:
            sys.stdout.write(self._text_buffer)
            text_lines = _count_visual_lines(self._text_buffer, term_width)
            if not self._text_buffer.endswith("\n"):
                # 让光标移到下一行,spinner 才能独占一行
                sys.stdout.write("\n")

        frame = _SPINNER_FRAMES[self._frame_idx % len(_SPINNER_FRAMES)]
        spinner_line = f"  {frame} {self.label}… ({self._fmt_status()})"
        sys.stdout.write(spinner_line)
        sys.stdout.flush()

        spinner_lines = max(1, (_display_width(spinner_line) + term_width - 1) // term_width)
        self._last_drawn_lines = text_lines + spinner_lines

    def _run(self) -> None:
        while not self._stop.wait(0.1):
            with self._lock:
                self._frame_idx += 1
                self._redraw()


class _PlainProgress:
    """非 TTY 时的退化实现 —— 不打动画，但仍支持流式文本和最终 token 摘要。"""

    def __init__(self, label: str):
        self.label = label
        self._t0 = 0.0
        self._metrics: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._text_started = False

    def __enter__(self) -> "_PlainProgress":
        self._t0 = time.monotonic()
        print(f"  ✻ {self.label}…", flush=True)
        return self

    def __exit__(self, *_) -> None:
        elapsed = time.monotonic() - self._t0
        out_t = self._metrics["output_tokens"]
        suffix = f" · ↓ {_fmt_tokens(out_t)}" if out_t else ""
        if self._text_started:
            sys.stdout.write("\n\n")
            sys.stdout.flush()
        print(f"  ✻ {self.label} ({_fmt_duration(elapsed)}{suffix})", flush=True)

    def update(self, metrics: dict[str, int]) -> None:
        self._metrics = {
            "input_tokens": int(metrics.get("input_tokens", 0) or 0),
            "output_tokens": int(metrics.get("output_tokens", 0) or 0),
        }

    def on_text(self, delta: str) -> None:
        if not delta:
            return
        if not self._text_started:
            self._text_started = True
            sys.stdout.write("\n")
        sys.stdout.write(delta)
        sys.stdout.flush()


@contextmanager
def _progress(label: str):
    if sys.stdout.isatty():
        with _Spinner(label) as s:
            yield s
    else:
        with _PlainProgress(label) as p:
            yield p


def _fmt(data: dict) -> str:
    try:
        return json.dumps(data, ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(data)


def _print_event(ev: TurnEvent) -> None:
    if ev.kind == "tool_call":
        print(f"  · tool {ev.tool_name}({_fmt(ev.tool_input)})", flush=True)
    elif ev.kind == "tool_result":
        preview = (ev.tool_output.splitlines()[:1] or [""])[0][:120]
        tag = "ERR" if ev.tool_error else "ok"
        t = f" ({ev.elapsed_seconds * 1000:.0f}ms)" if ev.elapsed_seconds < 1 else f" ({ev.elapsed_seconds:.1f}s)"
        print(f"    [{tag}]{t} {preview}", flush=True)
    elif ev.kind == "tool_denied":
        print(f"    [denied] 已拒绝 {ev.tool_name}", flush=True)
    elif ev.kind == "text":
        print(f"\n{ev.text}\n", flush=True)


def _build_session(auto_approve: bool, profile: str | None) -> AgentSession:
    cfg = load_config(profile_name=profile)
    if cfg.provider == "anthropic" and not (cfg.auth_token or cfg.api_key):
        print("未找到 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY。\n请在 .env 或 ~/.claude/settings.json 中配置后重试。", file=sys.stderr)
        sys.exit(2)

    llm = build_model_router(cfg)

    print("== AgentLab ==")
    print(f"provider : {cfg.provider}")
    print(f"model    : {cfg.model}")
    if cfg.base_url:
        print(f"base_url : {cfg.base_url}")
    if cfg.profile_name:
        print(f"profile  : {cfg.profile_name}")
    if cfg.capabilities:
        print(f"能力     : {', '.join(cfg.capabilities)}")
    from app.config.loader import workspace_root
    print(f"workspace: {workspace_root()}")
    print("工具     : read_file / write_file / list_dir")
    print("审批     : AUTO (-y)" if auto_approve else "审批     : 修改类工具会逐次询问 [y]这次 / [a]本会话总是 / [n]拒绝")
    print("输入 /reset 清空会话; exit/quit 或 Ctrl-D 退出; Ctrl-C 清空当前行.\n")

    registry = ToolRegistry()
    for tool in default_tools():
        registry.register(tool)

    # 启动校验:profile 声明了能力但没包含 "tools",而我们注册了工具 → 警告
    # (不阻断,用户可能想看模型如何降级表现;但提前知道比执行中崩溃好)
    if cfg.capabilities and "tools" not in cfg.capabilities and registry.all():
        print(
            f"⚠ profile '{cfg.profile_name}' 没有声明 tools 能力,"
            f"模型可能不会调用工具或直接报错。\n"
            f"   如果确认模型支持工具,在 config/models.yaml 的 capabilities 加上 'tools'。\n",
            file=sys.stderr,
        )

    return AgentSession(
        llm=llm,
        tools=registry,
        approval=AutoApprove() if auto_approve else InteractivePolicy(),
        on_event=_print_event,
        progress=_progress,
    )


def _print_stats(session: AgentSession) -> None:
    t, c = session.last_turn_usage, session.cumulative_usage
    print(
        f"  [stats] turn {session.last_turn_seconds:.1f}s "
        f"in={t['input_tokens']} out={t['output_tokens']} | "
        f"session {session.cumulative_seconds:.1f}s "
        f"in={c['input_tokens']} out={c['output_tokens']}",
        flush=True,
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


def _repl(session: AgentSession) -> int:
    """交互式对话。

    用 prompt_toolkit 替代内建 input(),解决:
      - 中文宽字符按退格只删 1 列、视觉残留的问题
      - 缺少历史回放(↑/↓)、Ctrl-A/E 编辑等
    Ctrl-C 清空当前行(不退出);Ctrl-D / exit / quit 退出。
    """
    pt_session: PromptSession = PromptSession(history=InMemoryHistory())
    prompt_fragments = FormattedText([("class:prompt", "▸ ")])

    while True:
        _print_input_separator()
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
            continue
        if line in ("exit", "quit"):
            return 0
        if line == "/reset":
            session.reset()
            print("(history cleared)")
            continue

        try:
            session.chat(line)
        except Exception as exc:
            # 脱敏后输出,避免 Authorization / API key 等凭据泄漏到终端
            print(f"  !! {format_exception(exc)}\n", file=sys.stderr)
            continue
        _print_stats(session)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentlab")
    parser.add_argument("-p", "--prompt", help="一次性 prompt，执行完即退出")
    parser.add_argument("-y", "--yes", action="store_true", help="自动放行所有工具调用")
    parser.add_argument("--profile", help="使用 config/models.yaml 中的指定 profile")
    args = parser.parse_args(argv)

    try:
        session = _build_session(auto_approve=args.yes, profile=args.profile)

        if args.prompt:
            try:
                session.chat(args.prompt)
            finally:
                _print_stats(session)
            return 0
        return _repl(session)
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
