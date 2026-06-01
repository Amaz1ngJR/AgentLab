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
from app.agent.tasks import TaskStore
from app.config.loader import load_config
from app.models.router import build_model_router
from app.tools.builtin import default_tools
from app.tools.builtin.todo import make_todo_write_tool
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


# 任务列表渲染用的 ANSI 颜色 / 字符
# 不用 prompt_toolkit Style 是为了让任务列表跟 spinner / 文本走同一条 stdout 流,
# 避免分别用 prompt_toolkit Application 与 ANSI 写入的协调问题
_ANSI_RESET = "\033[0m"
_ANSI_DIM = "\033[2;90m"        # 灰色 dim (completed 划过)
_ANSI_BLUE_BOLD = "\033[1;34m"  # 蓝色加粗 (in_progress 高亮)
_ANSI_BOLD = "\033[1m"           # 加粗 (汇总行)


def _format_task_lines(tasks) -> list[str]:
    """把 TaskStore 中的任务列表渲染成多行字符串(已包含 ANSI 颜色)。

    格式参考 Claude Code 的任务面板:
        4 tasks (1 done, 1 in progress, 2 open)
          ✓ workspace 路径限制         (灰色,已完成)
          ❯ 错误脱敏                    (蓝色加粗,进行中)
          ○ 工具能力声明                (普通色,待办)
          ○ AgentSession 离线测试

    返回的每一行 *不* 含末尾换行,调用方决定怎么拼。
    任务为空时返回空列表(让调用方决定是否显示标题区)。
    """
    if not tasks:
        return []

    counts = {"completed": 0, "in_progress": 0, "pending": 0}
    for t in tasks:
        if t.status in counts:
            counts[t.status] += 1

    header = (
        f"{_ANSI_BOLD}{len(tasks)} tasks{_ANSI_RESET} "
        f"({counts['completed']} done, "
        f"{counts['in_progress']} in progress, "
        f"{counts['pending']} open)"
    )
    lines = [header]

    for t in tasks:
        if t.status == "completed":
            lines.append(f"  {_ANSI_DIM}✓ {t.content}{_ANSI_RESET}")
        elif t.status == "in_progress":
            lines.append(f"  {_ANSI_BLUE_BOLD}❯ {t.content}{_ANSI_RESET}")
        else:
            lines.append(f"  ○ {t.content}")

    return lines


def _strip_ansi(s: str) -> str:
    """计算显示宽度时要先去掉 ANSI 转义,否则会把控制字符当成可见字符宽度。"""
    import re
    return re.sub(r"\033\[[0-9;]*m", "", s)


class _Spinner:
    """thinking 状态行 + 实时 token 计数 + 流式文本输出 + 任务列表面板。

    布局(从上到下):
      [任务列表]    ← 模型用 todo_write 维护,只在非空时显示
      [流式文本]    ← 流式 delta 累积
      [spinner]     ← ⠋ thinking… (...) 钉在最底,1Hz 闪动

    每次 token 更新或文本到达,都用 ANSI 重绘整个区域,保持 spinner 始终在
    最下面持续刷新。

    向 Runtime 暴露两个回调入口:
      update(metrics)  - token 进度更新
      on_text(delta)   - 文本增量;累积到 text_buffer,立即重绘
    """

    def __init__(self, label: str, task_store=None):
        self.label = label
        self._task_store = task_store  # None = 不显示任务面板
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._t0 = 0.0
        self._lock = threading.Lock()
        self._metrics: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._text_buffer = ""
        self._frame_idx = 0
        self._last_drawn_lines = 0  # 上次写入占用的屏幕行数(含任务+文本+spinner)

    def __enter__(self) -> "_Spinner":
        self._t0 = time.monotonic()
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        with self._lock:
            # 最终态:擦掉 spinner 区域,把任务列表和正文留在历史里
            self._erase_last()

            # 任务列表:即使本轮没新增/更新,只要存在就保留显示在历史里
            task_lines = self._task_lines()
            if task_lines:
                for line in task_lines:
                    sys.stdout.write(line + "\n")
                sys.stdout.write("\n")

            if self._text_buffer:
                sys.stdout.write(self._text_buffer)
                if not self._text_buffer.endswith("\n"):
                    sys.stdout.write("\n")
                sys.stdout.write("\n")
            else:
                # 没有文本(纯 tool_use),保留一行 thinking 摘要
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

    def _task_lines(self) -> list[str]:
        """从 task_store 取最新快照,渲染成行列表。store=None 或空时返回 []。"""
        if self._task_store is None:
            return []
        return _format_task_lines(self._task_store.all())

    def _erase_last(self) -> None:
        """擦掉上一次绘制的内容。光标在最后一行末尾,需要回到起点并清屏到末尾。"""
        if self._last_drawn_lines <= 0:
            return
        sys.stdout.write("\r")
        if self._last_drawn_lines > 1:
            sys.stdout.write(f"\033[{self._last_drawn_lines - 1}A")
        sys.stdout.write("\033[J")
        self._last_drawn_lines = 0

    def _redraw(self) -> None:
        """擦旧 → 写任务列表 → 写 text → 写 spinner。调用方需持有 self._lock。"""
        self._erase_last()

        term_width = _term_width()
        total_lines = 0

        # ── 任务列表(如果有) ──────────────────────────────────────────────
        task_lines = self._task_lines()
        if task_lines:
            for line in task_lines:
                sys.stdout.write(line + "\n")
                # _strip_ansi 去除颜色控制字符再算显示宽度,否则会被当成可见字符
                visible = _strip_ansi(line)
                w = _display_width(visible)
                total_lines += max(1, (w + term_width - 1) // term_width) if w else 1
            # 任务列表与文本之间留一行间隔
            sys.stdout.write("\n")
            total_lines += 1

        # ── 流式文本 ────────────────────────────────────────────────────────
        if self._text_buffer:
            sys.stdout.write(self._text_buffer)
            total_lines += _count_visual_lines(self._text_buffer, term_width)
            if not self._text_buffer.endswith("\n"):
                # 让光标移到下一行,spinner 才能独占一行
                sys.stdout.write("\n")

        # ── spinner 行 ──────────────────────────────────────────────────────
        frame = _SPINNER_FRAMES[self._frame_idx % len(_SPINNER_FRAMES)]
        spinner_line = f"  {frame} {self.label}… ({self._fmt_status()})"
        sys.stdout.write(spinner_line)
        sys.stdout.flush()

        spinner_lines = max(1, (_display_width(spinner_line) + term_width - 1) // term_width)
        self._last_drawn_lines = total_lines + spinner_lines

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


def _make_progress(task_store=None):
    """构造 progress 工厂(传给 AgentSession.progress)。

    把 task_store 通过闭包注入到 _Spinner,让它在重绘时能拿到最新任务列表。
    分成工厂是因为 AgentSession 需要的 progress 签名是 Callable[[label], CM],
    不能直接接受 task_store 参数。
    """
    @contextmanager
    def _progress(label: str):
        if sys.stdout.isatty():
            with _Spinner(label, task_store=task_store) as s:
                yield s
        else:
            with _PlainProgress(label) as p:
                yield p
    return _progress


# 兼容旧调用:模块顶层仍提供一个无 task_store 的默认 _progress
_progress = _make_progress(None)


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


def _build_session(auto_approve: bool, profile: str | None) -> AgentSession:
    cfg = load_config(profile_name=profile)
    if cfg.provider == "anthropic" and not (cfg.auth_token or cfg.api_key):
        print("未找到 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_API_KEY。\n请在 .env 或 ~/.claude/settings.json 中配置后重试。", file=sys.stderr)
        sys.exit(2)

    # 本地 profile:先 ping Ollama,不通就提前给安装引导(而不是等首次工具调用才报错)
    _check_local_endpoint(cfg)

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
    print("工具     : read_file / write_file / list_dir / shell / todo_write")
    print("审批     : AUTO (-y)" if auto_approve else "审批     : 修改类工具会方向键菜单确认 (允许这次 / 总是允许 / 拒绝)")
    print("输入 /reset 清空会话; exit/quit 或 Ctrl-D 退出; Ctrl-C 清空当前行.\n")

    # 任务清单(模型用 todo_write 维护,CLI spinner 区域上方实时渲染)
    task_store = TaskStore()

    registry = ToolRegistry()
    for tool in default_tools():
        registry.register(tool)
    # todo_write 工具需要绑定到当前会话的 task_store,所以工厂出来
    registry.register(make_todo_write_tool(task_store))

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
        progress=_make_progress(task_store),  # spinner 能拿到任务列表
        task_store=task_store,
    )


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
